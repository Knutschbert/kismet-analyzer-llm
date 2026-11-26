"""Dependency-free MCP stdio server for semantic kismet-analyzer output.

The server intentionally uses only Python's standard library so it can run from
the workspace without a machine-specific package installation. It implements
the MCP JSON-RPC methods needed by local clients: initialize, ping, tools/list,
and tools/call.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import time
from collections import OrderedDict, deque
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from itertools import islice
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger("kismet-analyzer-mcp")
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
# find_assets shards its text scan across this many threads. SQLite releases
# the GIL during query execution, so parallel rowid-range scans genuinely
# overlap instead of serializing.
SCAN_WORKERS = min(8, os.cpu_count() or 1)
# Bump when the index schema changes; a mismatched index is wiped and rebuilt.
# 2: adds the precomputed lowercase `search` column for find_assets.
# 3: adds the `parent` column + index for class-hierarchy queries.
# 4: leaner flattened search blob (name/value text instead of raw JSON).
# 5: adds the exported_objects table for schema-v2 semantic documents.
# 6: records which assets actually carry exported-object inventories so v1 documents
#    are reported as unknown rather than as an empty inventory.
# 7: excludes verbose, low-value cooked metadata from the exported-object search blob.
SCHEMA_VERSION = "7"

# These properties are retained in the detailed exportedObjects properties payload but are
# omitted from the global search text. They are high-volume cooked/build metadata that is not
# useful for class, map, or spawner discovery queries.
SEARCH_EXCLUDED_PROPERTIES = {
    "AggGeom",
    "AlternateSkinWeights",
    "BodyInstance",
    "CreationMethod",
    "PrecompiledEvaluationTemplate",
    "SplineCurves",
    "StaticMeshComponent",
    "StreamingTextureData",
    "UCSModifiedProperties",
    "UCSSerializationIndex",
    "bNetAddressable",
}


def _json_result(value: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(value, indent=2)}], "structuredContent": value}


def _error_result(message: str) -> dict[str, Any]:
    return {
        "isError": True,
        "content": [{"type": "text", "text": message}],
    }


def _scan_shard_worker(task: tuple[str, int, int, str]) -> list[str]:
    """Scan one rowid shard of the search text; runs in a worker process.

    Threads cannot parallelize this scan: the GIL is released per step but
    the LIKE work and connection locks serialize in practice (measured:
    4 threads ≈ sequential). Worker processes are the only way to overlap
    the scan across cores. mmap avoids the per-connection page-cache copy,
    which alone is worth ~1.5x on a full scan.
    """
    index_path, shard_lo, shard_hi, needle = task
    with sqlite3.connect(index_path) as conn:
        conn.execute("PRAGMA mmap_size=536870912")
        return [
            row[0]
            for row in conn.execute(
                "SELECT asset FROM assets WHERE rowid BETWEEN ? AND ? AND search LIKE ?",
                (shard_lo, shard_hi, needle),
            ).fetchall()
        ]


def _flatten_value(value: Any, parts: list[str]) -> None:
    """Collect searchable scalar text from a property/default-value item.

    Strings and numbers are gathered recursively from nested containers while
    JSON punctuation and repeated structural keys ("name", "type", "value")
    are dropped. That shrinks the scanned search text — the dominant
    find_assets cost — without removing any name or value an agent matches
    against.
    """
    if isinstance(value, str):
        if value:
            parts.append(value)
    elif isinstance(value, bool):
        parts.append(str(value).lower())
    elif isinstance(value, (int, float)):
        parts.append(repr(value))
    elif isinstance(value, dict):
        for inner in value.values():
            _flatten_value(inner, parts)
    elif isinstance(value, (list, tuple)):
        for inner in value:
            _flatten_value(inner, parts)


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS assets (
    asset TEXT PRIMARY KEY, class TEXT, parent TEXT, output TEXT, parse_status TEXT,
    reason TEXT, functions TEXT NOT NULL, properties TEXT NOT NULL,
    default_values TEXT NOT NULL DEFAULT '[]',
    search TEXT NOT NULL DEFAULT '',
    exported_objects_available INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS functions (
    asset TEXT NOT NULL, name TEXT NOT NULL, calls TEXT NOT NULL,
    reads TEXT NOT NULL, writes TEXT NOT NULL, branches INTEGER NOT NULL,
    diagnostics TEXT NOT NULL, PRIMARY KEY(asset, name)
);
CREATE TABLE IF NOT EXISTS references_index (
    asset TEXT NOT NULL, function TEXT NOT NULL, target TEXT NOT NULL,
    kind TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exported_objects (
    asset TEXT NOT NULL, path TEXT NOT NULL, name TEXT NOT NULL, class TEXT,
    outer TEXT, properties TEXT NOT NULL, search TEXT NOT NULL,
    PRIMARY KEY(asset, path)
);
CREATE INDEX IF NOT EXISTS references_target ON references_index(target);
CREATE INDEX IF NOT EXISTS assets_parent ON assets(parent);
CREATE INDEX IF NOT EXISTS exported_objects_class ON exported_objects(class);
CREATE INDEX IF NOT EXISTS exported_objects_name ON exported_objects(name);
"""


class Catalog:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.manifest_path = self.root / "manifest.json"
        configured_index = os.environ.get("KISMET_MCP_INDEX")
        self.index_path = Path(configured_index).expanduser() if configured_index else self.root / ".mcp-index.sqlite3"
        self.entries: list[dict[str, Any]] = []
        self.by_asset: dict[str, dict[str, Any]] = {}
        # LRU of scan results keyed by needle: the index is static between
        # refreshes and agents retry query variations, so repeats are instant.
        self._scan_cache: OrderedDict[str, list[str]] = OrderedDict()
        # Worker processes for the sharded scan; created lazily on the first
        # parallel scan so small catalogs (and tests) never pay for them.
        self._scan_pool: ProcessPoolExecutor | None = None
        # Cached byte-balanced rowid shards for the scan (see _scan_shards).
        self._scan_bounds: list[tuple[int, int]] | None = None
        self.refresh()

    @staticmethod
    def workspace_root() -> Path:
        return Path(__file__).resolve().parent.parent

    def refresh(self) -> None:
        self._scan_cache.clear()
        self._scan_bounds = None
        if not self.manifest_path.is_file():
            raise FileNotFoundError(f"Manifest not found: {self.manifest_path}")
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.entries = manifest.get("assets", [])
        self.by_asset = {
            self._normalize(entry.get("asset", "")): entry
            for entry in self.entries
            if entry.get("asset")
        }
        self.refresh_index()

    def refresh_index(self) -> None:
        """Build or refresh the local SQLite catalog when the manifest or schema changes."""
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_mtime = self.manifest_path.stat().st_mtime_ns
        with sqlite3.connect(self.index_path) as db:
            # The index is a derived cache that is cheap to rebuild; durable
            # writes only add latency on slow mounts.
            db.execute("PRAGMA synchronous=OFF")
            # Bootstrap only the meta table, then check the schema version
            # before creating anything else: an older index may lack new
            # columns, and CREATE TABLE IF NOT EXISTS does not add them.
            db.executescript(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            )
            version = db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            if not version or version[0] != SCHEMA_VERSION:
                # Drop and re-derive: cheaper and safer than in-place backfills.
                db.executescript(
                    "DROP TABLE IF EXISTS references_index;"
                    "DROP TABLE IF EXISTS exported_objects;"
                    "DROP TABLE IF EXISTS functions;"
                    "DROP TABLE IF EXISTS assets;"
                    "DROP TABLE IF EXISTS meta;"
                )
                db.executescript(SCHEMA)
                db.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                    (SCHEMA_VERSION,),
                )
            else:
                db.executescript(SCHEMA)

            previous = db.execute("SELECT value FROM meta WHERE key = 'manifest_mtime'").fetchone()
            if previous and previous[0] == str(manifest_mtime):
                return

            db.execute("DELETE FROM assets")
            db.execute("DELETE FROM functions")
            db.execute("DELETE FROM references_index")
            db.execute("DELETE FROM exported_objects")

            def _safe_load(entry: dict[str, Any]) -> dict[str, Any]:
                try:
                    return self._load_output(entry["output"])
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    LOGGER.warning("Unable to index %s: %s", entry.get("asset", ""), exc)
                    return {}

            def _index_entry(entry: dict[str, Any], document: dict[str, Any]) -> None:
                asset = entry.get("asset", "")
                output = entry.get("output")
                properties = document.get("properties", [])
                functions = document.get("functions", [])
                default_values = document.get("defaultValues", [])
                exported_objects = document.get("exportedObjects", [])
                functions_blob = json.dumps(entry.get("functions", [f.get("name") for f in functions]))
                properties_blob = json.dumps(properties)
                defaults_blob = json.dumps(default_values)
                # Flattened name/value text for the search blob instead of raw
                # JSON: punctuation and repeated structural keys add tens of
                # percent of scan volume without adding matchable content.
                property_parts: list[str] = []
                for item in properties:
                    _flatten_value(item, property_parts)
                default_parts: list[str] = []
                for item in default_values:
                    _flatten_value(item, default_parts)
                class_name = entry.get("class") or document.get("class") or ""
                parent_name = document.get("parent")
                # Precomputed lowercase search text: find_assets scans this
                # column directly instead of re-concatenating and lowercasing
                # every row's full JSON text per query. Parent is included so
                # searching a parent class name finds its subclasses.
                search_blob = " ".join(
                    [asset, class_name, parent_name or "", functions_blob]
                    + property_parts
                    + default_parts
                ).lower()
                db.execute(
                    "INSERT INTO assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        asset,
                        class_name,
                        parent_name,
                        output,
                        entry.get("parseStatus", document.get("parseStatus", "unknown")),
                        entry.get("reason"),
                        functions_blob,
                        properties_blob,
                        defaults_blob,
                        search_blob,
                        1 if "exportedObjects" in document else 0,
                    ),
                )
                for function in functions:
                    name = function.get("name", "")
                    calls = function.get("calls", [])
                    db.execute(
                        "INSERT OR REPLACE INTO functions VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            asset,
                            name,
                            json.dumps(calls),
                            json.dumps(function.get("reads", [])),
                            json.dumps(function.get("writes", [])),
                            int(function.get("branches", 0)),
                            json.dumps(function.get("diagnostics", [])),
                        ),
                    )
                    for target in calls:
                        db.execute(
                            "INSERT INTO references_index VALUES (?, ?, ?, ?)",
                            (asset, name, target, "function_call"),
                        )
                    for target in function.get("reads", []):
                        db.execute(
                            "INSERT INTO references_index VALUES (?, ?, ?, ?)",
                            (asset, name, target, "property_read"),
                        )
                    for target in function.get("writes", []):
                        db.execute(
                            "INSERT INTO references_index VALUES (?, ?, ?, ?)",
                            (asset, name, target, "property_write"),
                        )
                for obj in exported_objects:
                    properties = obj.get("properties", [])
                    parts: list[str] = []
                    for key, value in obj.items():
                        if key != "properties":
                            _flatten_value(value, parts)
                    for item in properties:
                        if item.get("name") not in SEARCH_EXCLUDED_PROPERTIES:
                            _flatten_value(item, parts)
                    db.execute(
                        "INSERT OR REPLACE INTO exported_objects VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            asset,
                            obj.get("path") or obj.get("name", ""),
                            obj.get("name", ""),
                            obj.get("class"),
                            obj.get("outer"),
                            json.dumps(properties),
                            " ".join(parts).lower(),
                        ),
                    )

            # Doc reads release the GIL, so a small thread pool overlaps the
            # per-request latency that dominates slow mounts (9p). JSON
            # parsing stays GIL-bound, so on fast local disks the win is small.
            # Chunked so only one chunk of parsed docs sits in memory at a time.
            with_output = [entry for entry in self.entries if entry.get("output")]
            pending = iter(with_output)
            with ThreadPoolExecutor(max_workers=8) as pool:
                while True:
                    chunk = list(islice(pending, 256))
                    if not chunk:
                        break
                    for entry, document in zip(chunk, pool.map(_safe_load, chunk)):
                        _index_entry(entry, document)
            # Empty assets stay indexed even without a document.
            for entry in self.entries:
                if not entry.get("output"):
                    _index_entry(entry, {})

            db.execute(
                "INSERT INTO meta(key, value) VALUES('manifest_mtime', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(manifest_mtime),),
            )
            db.commit()

    def _load_output(self, output: str) -> dict[str, Any]:
        # normpath instead of resolve(): resolve() lstats every path component,
        # which costs minutes across thousands of docs on slow mounts (9p).
        # The generated tree contains no symlinks, so a lexical containment
        # check is sufficient here.
        path = os.path.normpath(self.root / output)
        root = str(self.root)
        if not path.startswith(root + os.sep):
            raise ValueError("Indexed output escapes the catalog root")
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def index_stats(self) -> dict[str, int]:
        with sqlite3.connect(self.index_path) as db:
            assets = db.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
            semantic = db.execute("SELECT COUNT(*) FROM assets WHERE output IS NOT NULL").fetchone()[0]
            functions = db.execute("SELECT COUNT(*) FROM functions").fetchone()[0]
            references = db.execute("SELECT COUNT(*) FROM references_index").fetchone()[0]
        return {"assets": assets, "semanticAssets": semantic, "functions": functions, "references": references}

    @staticmethod
    def _normalize(asset: str) -> str:
        return asset.replace("\\", "/").lstrip("/").casefold()

    def find_assets(
        self,
        query: str,
        limit: int,
        offset: int = 0,
        include_descendants: bool = False,
        max_nodes: int = 500,
    ) -> dict[str, Any]:
        needle = f"%{query.casefold().strip()}%"
        # One sharded text scan per call: collect the matching keys, then
        # detail-fetch only the requested page by primary key. A separate
        # COUNT(*) would rescan the full search text a second time.
        keys = self._scan_keys(needle)
        descendants_truncated = False
        if include_descendants and keys:
            # Expand every matched class's subtree through the parent index.
            # Matched keys are staged in a temp table so the recursive CTE
            # reuses the sharded scan instead of rescanning the search text.
            # Fetch one extra row to detect truncation.
            with sqlite3.connect(self.index_path) as db:
                db.execute("CREATE TEMP TABLE IF NOT EXISTS matched_keys (asset TEXT PRIMARY KEY)")
                db.execute("DELETE FROM matched_keys")
                db.executemany("INSERT INTO matched_keys VALUES (?)", [(key,) for key in keys])
                expanded = db.execute(
                    """
                    WITH RECURSIVE tree(asset, class) AS (
                        SELECT asset, class FROM assets
                        WHERE asset IN (SELECT asset FROM matched_keys)
                        UNION
                        SELECT a.asset, a.class FROM assets a JOIN tree t ON a.parent = t.class
                    )
                    SELECT asset FROM tree ORDER BY asset LIMIT ?
                    """,
                    (max_nodes + 1,),
                ).fetchall()
            descendants_truncated = len(expanded) > max_nodes
            keys = [row[0] for row in expanded[:max_nodes]]
        count = len(keys)
        with sqlite3.connect(self.index_path) as db:
            rows: list[tuple[Any, ...]] = []
            page_keys = keys[offset : offset + limit]
            if page_keys:
                placeholders = ",".join("?" * len(page_keys))
                rows = db.execute(
                    "SELECT asset, class, output, parse_status, functions, default_values "
                    f"FROM assets WHERE asset IN ({placeholders}) ORDER BY asset",
                    page_keys,
                ).fetchall()
        page = [
            {
                "asset": asset,
                "class": class_name,
                "functions": json.loads(functions),
                "parseStatus": parse_status,
                "hasSemanticOutput": bool(output),
                # default_values is a JSON array; '[]' means empty. Do NOT
                # json.loads it here: Falmire-scale map rows carry
                # multi-megabyte default blobs and parsing them per page row
                # dominated cached-call latency.
                "hasDefaultValues": default_values not in ("", "[]", "null"),
            }
            for asset, class_name, output, parse_status, functions, default_values in rows
        ]
        next_offset = offset + limit if offset + limit < count else None
        result = {
            "query": query,
            "results": page,
            "count": count,
            "nextOffset": next_offset,
        }
        if include_descendants:
            result["descendantsTruncated"] = descendants_truncated
        return result

    def _scan_shards(self) -> list[tuple[int, int]]:
        """Rowid ranges balanced by search-text bytes, not by row count.

        Search text is heavily skewed — map rows carry megabytes of defaults
        clustered in a few rowid ranges — so count-balanced shards leave one
        worker holding most of the bytes and the pool just waits on it.
        Boundaries are cached: the index is static between refreshes.
        """
        if self._scan_bounds is not None:
            return self._scan_bounds
        with sqlite3.connect(self.index_path) as conn:
            sizes = conn.execute(
                "SELECT rowid, LENGTH(search) FROM assets ORDER BY rowid"
            ).fetchall()
        shards: list[tuple[int, int]] = []
        if sizes:
            total = sum(size or 0 for _, size in sizes)
            target = total / SCAN_WORKERS
            acc = 0
            start = sizes[0][0]
            next_cut = target
            for rowid, size in sizes:
                acc += size or 0
                if acc >= next_cut and len(shards) < SCAN_WORKERS - 1:
                    shards.append((start, rowid))
                    start = rowid + 1
                    next_cut += target
            shards.append((start, sizes[-1][0]))
        self._scan_bounds = shards
        return shards

    def _scan_keys(self, needle: str) -> list[str]:
        """Scan the search text for matching asset keys.

        The scan is sharded across worker processes (threads and processes
        alike only help when shards carry similar byte counts — see
        _scan_shards), balanced by search-text bytes. Shards are disjoint,
        so the merged keys only need one sort. Results are cached per
        needle: the index is static between refreshes and agents retry
        query variations.
        """
        cached = self._scan_cache.get(needle)
        if cached is not None:
            self._scan_cache.move_to_end(needle)
            return cached
        with sqlite3.connect(self.index_path) as db:
            total = db.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        if not total:
            keys: list[str] = []
        elif total < 5000 or SCAN_WORKERS <= 1:
            # Small catalogs (fixtures): sharding overhead is not worth it.
            with sqlite3.connect(self.index_path) as conn:
                conn.execute("PRAGMA mmap_size=536870912")
                keys = [
                    row[0]
                    for row in conn.execute(
                        "SELECT asset FROM assets WHERE search LIKE ? ORDER BY asset", (needle,)
                    ).fetchall()
                ]
        else:
            shards = self._scan_shards()
            tasks = [(str(self.index_path), shard_lo, shard_hi, needle) for shard_lo, shard_hi in shards]
            if self._scan_pool is None:
                self._scan_pool = ProcessPoolExecutor(max_workers=SCAN_WORKERS)
            keys = []
            for part in self._scan_pool.map(_scan_shard_worker, tasks):
                keys.extend(part)
            keys.sort()
        if len(keys) <= 10000:  # broad scans produce huge lists; skip caching
            self._scan_cache[needle] = keys
            while len(self._scan_cache) > 64:
                self._scan_cache.popitem(last=False)
        return keys

    def get_lineage(self, asset: str, max_children: int, max_siblings: int) -> dict[str, Any]:
        """Class hierarchy for one asset: ancestry, direct children, siblings."""
        with sqlite3.connect(self.index_path) as db:
            row = db.execute(
                "SELECT asset, class, parent FROM assets WHERE lower(asset) = ?",
                (self._normalize(asset),),
            ).fetchone()
            if row is None:
                # Allow lookup by class name, with or without the `_C` suffix.
                needle = asset.strip().casefold()
                candidates = [needle] if needle.endswith("_c") else [needle, needle + "_c"]
                for candidate in candidates:
                    row = db.execute(
                        "SELECT asset, class, parent FROM assets WHERE lower(class) = ?",
                        (candidate,),
                    ).fetchone()
                    if row is not None:
                        break
            if row is None:
                raise KeyError(f"Asset is not indexed: {asset}")
            path, class_name, parent = row
            if not class_name:
                raise KeyError(f"Asset has no class: {path}")

            def related(where_parent: str, exclude: str | None, limit: int) -> tuple[list, int, bool]:
                if exclude:
                    total = db.execute(
                        "SELECT COUNT(*) FROM assets WHERE parent = ? AND lower(class) != ?",
                        (where_parent, exclude.casefold()),
                    ).fetchone()[0]
                    rows = db.execute(
                        "SELECT asset, class FROM assets WHERE parent = ? AND lower(class) != ? "
                        "ORDER BY asset LIMIT ?",
                        (where_parent, exclude.casefold(), limit),
                    ).fetchall()
                else:
                    total = db.execute(
                        "SELECT COUNT(*) FROM assets WHERE parent = ?", (where_parent,)
                    ).fetchone()[0]
                    rows = db.execute(
                        "SELECT asset, class FROM assets WHERE parent = ? ORDER BY asset LIMIT ?",
                        (where_parent, limit),
                    ).fetchall()
                items = [{"asset": item, "class": name} for item, name in rows]
                return items, total, total > len(items)

            children, child_total, children_truncated = related(class_name, None, max_children)
            if parent:
                siblings, sibling_total, siblings_truncated = related(parent, class_name, max_siblings)
            else:
                siblings, sibling_total, siblings_truncated = [], 0, False

            # Ancestry: walk upward while the parent resolves to an indexed
            # asset. Native classes (e.g. Actor) terminate the chain honestly.
            ancestry = [{"class": class_name, "asset": path, "inCatalog": True}]
            ancestry_truncated = False
            seen = {class_name.casefold()}
            cursor = parent
            while cursor:
                key = cursor.casefold()
                if key in seen:  # cycle guard
                    break
                seen.add(key)
                nxt = db.execute(
                    "SELECT asset, class, parent FROM assets WHERE lower(class) = ?", (key,)
                ).fetchone()
                if nxt is None:
                    ancestry.append({"class": cursor, "asset": None, "inCatalog": False})
                    break
                ancestry.append({"class": nxt[1], "asset": nxt[0], "inCatalog": True})
                cursor = nxt[2]
                if len(ancestry) >= 32:
                    ancestry_truncated = True
                    break

        return {
            "asset": path,
            "class": class_name,
            "parent": parent,
            "ancestry": ancestry,
            "ancestryTruncated": ancestry_truncated,
            "children": children,
            "childCount": child_total,
            "childrenTruncated": children_truncated,
            "siblings": siblings,
            "siblingCount": sibling_total,
            "siblingsTruncated": siblings_truncated,
        }

    def get_entry(self, asset: str) -> dict[str, Any] | None:
        return self.by_asset.get(self._normalize(asset))

    def load_asset(self, asset: str) -> dict[str, Any]:
        entry = self.get_entry(asset)
        if entry is None:
            raise KeyError(f"Asset is not indexed: {asset}")
        output = entry.get("output")
        if not output:
            return {
                "asset": entry.get("asset"),
                "class": entry.get("class"),
                "parseStatus": entry.get("parseStatus", "empty"),
                "reason": entry.get("reason"),
                "functions": [],
                "properties": [],
                "diagnostics": [],
            }

        path = (self.root / output).resolve()
        if self.root not in path.parents:
            raise ValueError("Indexed output escapes the catalog root")
        if not path.is_file():
            raise FileNotFoundError(f"Indexed semantic document not found: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _exported_objects_available(document: dict[str, Any]) -> bool:
        return "exportedObjects" in document

    @staticmethod
    def _compact_exported_object(obj: dict[str, Any]) -> dict[str, Any]:
        properties = obj.get("properties", [])
        return {
            "path": obj.get("path") or obj.get("name", ""),
            "name": obj.get("name", ""),
            "class": obj.get("class"),
            "outer": obj.get("outer"),
            "propertyCount": len(properties),
        }

    def list_exported_objects(
        self,
        asset: str,
        class_name: str,
        name: str,
        outer: str,
        path: str,
        property_name: str,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        document = self.load_asset(asset)
        available = self._exported_objects_available(document)
        objects = document.get("exportedObjects", [])
        needles = {
            "className": class_name.casefold(),
            "name": name.casefold(),
            "outer": outer.casefold(),
            "path": path.casefold(),
            "propertyName": property_name.casefold(),
        }
        matching = []
        for obj in objects:
            properties = obj.get("properties", [])
            property_names = [str(item.get("name", "")).casefold() for item in properties]
            if needles["className"] and needles["className"] not in str(obj.get("class") or "").casefold():
                continue
            if needles["name"] and needles["name"] not in str(obj.get("name", "")).casefold():
                continue
            if needles["outer"] and needles["outer"] not in str(obj.get("outer") or "").casefold():
                continue
            object_path = str(obj.get("path") or obj.get("name", ""))
            if needles["path"] and needles["path"] not in object_path.casefold():
                continue
            if needles["propertyName"] and not any(needles["propertyName"] in item for item in property_names):
                continue
            matching.append(obj)
        return {
            "asset": document.get("asset", asset),
            "schemaVersion": document.get("schemaVersion"),
            "exportedObjectsAvailable": available,
            "total": len(matching),
            "offset": offset,
            "limit": limit,
            "results": [self._compact_exported_object(obj) for obj in matching[offset : offset + limit]],
            "coverageNote": None if available else "Schema-v1 documents have no exported-object inventory; absence is unknown, not empty.",
        }

    def get_exported_object(
        self, asset: str, object_path: str, max_properties: int
    ) -> dict[str, Any]:
        document = self.load_asset(asset)
        available = self._exported_objects_available(document)
        objects = document.get("exportedObjects", [])
        matches = [
            obj
            for obj in objects
            if str(obj.get("path") or obj.get("name", "")) == object_path
        ]
        if not matches:
            matches = [obj for obj in objects if str(obj.get("name", "")) == object_path]
        base = {
            "asset": document.get("asset", asset),
            "schemaVersion": document.get("schemaVersion"),
            "exportedObjectsAvailable": available,
            "objectPath": object_path,
            "coverageNote": None if available else "Schema-v1 documents have no exported-object inventory; absence is unknown, not empty.",
        }
        if not matches:
            return {**base, "found": False, "exportedObject": None}
        if len(matches) > 1:
            return {**base, "found": True, "ambiguous": True, "exportedObject": None}
        obj = matches[0]
        properties = obj.get("properties", [])
        exported = dict(obj)
        exported["properties"] = properties[:max_properties]
        return {
            **base,
            "found": True,
            "ambiguous": False,
            "exportedObject": exported,
            "propertiesTruncated": len(properties) > max_properties,
        }

    def find_exported_objects(
        self,
        query: str,
        class_name: str,
        class_exact: str,
        asset_prefix: str,
        property_name: str,
        property_value: str,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if query:
            clauses.append("search LIKE ?")
            params.append(f"%{query.casefold()}%")
        if class_name:
            clauses.append("lower(class) LIKE ?")
            params.append(f"%{class_name.casefold()}%")
        if class_exact:
            clauses.append("class = ?")
            params.append(class_exact)
        if asset_prefix:
            clauses.append("lower(asset) LIKE ?")
            params.append(f"{asset_prefix.casefold()}%")
        if property_name:
            clauses.append("lower(properties) LIKE ?")
            params.append(f"%{property_name.casefold()}%")
        if property_value:
            clauses.append("lower(properties) LIKE ?")
            params.append(f"%{property_value.casefold()}%")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with sqlite3.connect(self.index_path) as db:
            total = db.execute(f"SELECT COUNT(*) FROM exported_objects{where}", params).fetchone()[0]
            rows = db.execute(
                f"SELECT asset, path, name, class, outer, properties FROM exported_objects{where} "
                "ORDER BY asset, path LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
            assets_total = db.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
            assets_with_objects = db.execute(
                "SELECT COUNT(*) FROM assets WHERE exported_objects_available = 1"
            ).fetchone()[0]
        return {
            "query": query,
            "className": class_name,
            "classExact": class_exact,
            "assetPrefix": asset_prefix,
            "propertyName": property_name,
            "propertyValue": property_value,
            "total": total,
            "offset": offset,
            "limit": limit,
            "results": [
                {
                    "asset": asset,
                    "path": path,
                    "name": name,
                    "class": class_value,
                    "outer": outer,
                    "propertyCount": len(json.loads(properties)),
                }
                for asset, path, name, class_value, outer, properties in rows
            ],
            "coverage": {
                "totalAssets": assets_total,
                "assetsWithExportedObjects": assets_with_objects,
                "note": "Schema-v1 documents are not searched; their object inventories are Unknown, not empty.",
            },
        }

    def find_references(self, query: str, limit: int) -> dict[str, Any]:
        needle = f"%{query.casefold()}%"
        with sqlite3.connect(self.index_path) as db:
            rows = db.execute(
                "SELECT asset, function, target, kind FROM references_index "
                "WHERE lower(target) LIKE ? OR lower(asset) LIKE ? OR lower(function) LIKE ? "
                "ORDER BY asset, function, target LIMIT ?",
                (needle, needle, needle, limit),
            ).fetchall()
        return {
            "query": query,
            "results": [
                {"asset": asset, "function": function, "target": target, "kind": kind}
                for asset, function, target, kind in rows
            ],
            "limit": limit,
            "truncated": len(rows) == limit,
        }

    @staticmethod
    def _manifest_summary(entry: dict[str, Any]) -> dict[str, Any]:
        return {
            "asset": entry.get("asset"),
            "class": entry.get("class"),
            "functions": entry.get("functions", []),
            "parseStatus": entry.get("parseStatus"),
            "hasSemanticOutput": bool(entry.get("output")),
        }


def default_catalog_root() -> Path:
    configured = os.environ.get("KISMET_LLM_DATA")
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parent.parent / "output" / "generated" / "tree" / "TBL"


def bounded_limit(value: Any) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(limit, MAX_LIMIT))


TOOLS = [
    {
        "name": "find_assets",
        "description": "Find indexed assets by path, class, or function name; optionally expand each matched class's descendant subtree.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Text to search for."},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": DEFAULT_LIMIT},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "includeDescendants": {
                    "type": "boolean",
                    "default": False,
                    "description": "Also return all descendant classes of every matched class (fuzzy class + subtree search).",
                },
                "maxNodes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5000,
                    "default": 500,
                    "description": "Bound on the expanded result set when includeDescendants is set.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_lineage",
        "description": "Get one asset's class hierarchy: parent class, ancestry chain, direct child classes, and class siblings.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "asset": {"type": "string", "description": "Indexed asset path or class name (with or without the _C suffix)."},
                "maxChildren": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": 50},
                "maxSiblings": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": 50},
            },
            "required": ["asset"],
        },
    },
    {
        "name": "get_asset_summary",
        "description": "Get concise semantic metadata, properties, defaults, exported objects, functions, and diagnostics for one asset.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "asset": {"type": "string", "description": "Indexed asset path."},
                "maxProperties": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": 50},
                "maxFunctions": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": 50},
                "maxDefaults": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": 50},
                "maxExportedObjects": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": 50},
            },
            "required": ["asset"],
        },
    },
    {
        "name": "list_exported_objects",
        "description": "List placed actors, components, and instanced subobjects in one asset, with their resolved classes and stable object paths.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "asset": {"type": "string", "description": "Indexed asset path."},
                "className": {"type": "string", "description": "Substring match against the export class."},
                "name": {"type": "string", "description": "Substring match against the object name."},
                "outer": {"type": "string", "description": "Substring match against the outer object name."},
                "path": {"type": "string", "description": "Substring match against the package-local object path."},
                "propertyName": {"type": "string", "description": "Require a property whose name contains this text."},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": DEFAULT_LIMIT},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            },
            "required": ["asset"],
        },
    },
    {
        "name": "get_exported_object",
        "description": "Get one placed actor, component, or instanced subobject by stable object path, including bounded properties.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "asset": {"type": "string", "description": "Indexed asset path."},
                "path": {"type": "string", "description": "Package-local object path; a unique object name is accepted as a fallback."},
                "maxProperties": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": 50},
            },
            "required": ["asset", "path"],
        },
    },
    {
        "name": "find_exported_objects",
        "description": "Find exported objects across the catalog by class, name/path text, asset prefix, or property name/value.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Text to search in object identity and property text."},
                "className": {"type": "string", "description": "Substring match against the export class; uses a full scan."},
                "classExact": {"type": "string", "description": "Exact class match; uses the class index."},
                "assetPrefix": {"type": "string", "description": "Restrict to asset paths with this prefix."},
                "propertyName": {"type": "string", "description": "Require a property whose name contains this text."},
                "propertyValue": {"type": "string", "description": "Require a property whose rendered value contains this text."},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": DEFAULT_LIMIT},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            },
            "required": [],
        },
    },
    {
        "name": "get_function_summary",
        "description": "Get one function's calls, variable access, branches, diagnostics, and optionally bounded CFG blocks.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "asset": {"type": "string"},
                "function": {"type": "string"},
                "includeBlocks": {"type": "boolean", "default": False},
                "maxBlocks": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": 20},
            },
            "required": ["asset", "function"],
        },
    },
    {
        "name": "find_references",
        "description": "Find function-call and property read/write references in the indexed semantic catalog.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": DEFAULT_LIMIT},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_catalog_stats",
        "description": "Return counts for the loaded asset catalog and semantic index.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_call_graph",
        "description": "Traverse bounded local function callers/callees for one asset.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "asset": {"type": "string"},
                "function": {"type": "string"},
                "direction": {"type": "string", "enum": ["callees", "callers", "both"], "default": "callees"},
                "maxDepth": {"type": "integer", "minimum": 0, "maximum": 5, "default": 2},
                "maxNodes": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": 30},
            },
            "required": ["asset", "function"],
        },
    },
    {
        "name": "get_control_flow",
        "description": "Return bounded CFG blocks and successors for one function.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "asset": {"type": "string"},
                "function": {"type": "string"},
                "maxBlocks": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": 20},
            },
            "required": ["asset", "function"],
        },
    },
    {
        "name": "trace_behavior",
        "description": "Find candidate legacy assets/functions and return bounded behavior evidence.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "asset": {"type": "string"},
                "function": {"type": "string"},
                "maxDepth": {"type": "integer", "minimum": 0, "maximum": 5, "default": 2},
                "maxNodes": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": 20},
            },
            "required": ["query"],
        },
    },
    {
        "name": "compare_assets",
        "description": "Compare two semantic assets by class, properties, functions, calls, and diagnostics.",
        "inputSchema": {
            "type": "object",
            "properties": {"left": {"type": "string"}, "right": {"type": "string"}},
            "required": ["left", "right"],
        },
    },
    {
        "name": "get_implementation_spec",
        "description": "Derive an evidence-backed implementation specification for recreating one legacy function in a new actor.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "asset": {"type": "string"},
                "function": {"type": "string"},
                "targetActor": {"type": "string"},
                "maxBlocks": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": 20},
            },
            "required": ["asset", "function"],
        },
    },
    {
        "name": "refresh_catalog",
        "description": "Refresh the local SQLite index from the configured manifest.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class McpServer:
    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        # (tool name, elapsed ms) for the last 25 calls; surfaced through
        # get_catalog_stats so call timings survive without log capture.
        self._recent_calls: deque[tuple[str, float]] = deque(maxlen=25)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Timed wrapper: records per-call durations for get_catalog_stats."""
        started = time.perf_counter()
        try:
            return self._dispatch(name, arguments)
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            self._recent_calls.append((name, elapsed_ms))
            LOGGER.info("tool %s -> %.1f ms", name, elapsed_ms)

    def _dispatch(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "find_assets":
            max_nodes = int(arguments.get("maxNodes", 500))
            return _json_result(
                self.catalog.find_assets(
                    str(arguments.get("query", "")),
                    bounded_limit(arguments.get("limit")),
                    max(0, int(arguments.get("offset", 0))),
                    include_descendants=bool(arguments.get("includeDescendants", False)),
                    max_nodes=max(1, min(max_nodes, 5000)),
                )
            )

        if name == "get_lineage":
            return _json_result(
                self.catalog.get_lineage(
                    str(arguments.get("asset", "")),
                    bounded_limit(arguments.get("maxChildren", 50)),
                    bounded_limit(arguments.get("maxSiblings", 50)),
                )
            )

        if name == "get_asset_summary":
            asset = str(arguments.get("asset", ""))
            document = self.catalog.load_asset(asset)
            max_properties = bounded_limit(arguments.get("maxProperties", 50))
            max_functions = bounded_limit(arguments.get("maxFunctions", 50))
            max_defaults = bounded_limit(arguments.get("maxDefaults", 50))
            max_exported_objects = bounded_limit(arguments.get("maxExportedObjects", 50))
            properties = document.get("properties", [])
            functions = document.get("functions", [])
            default_values = document.get("defaultValues", [])
            exported_objects = document.get("exportedObjects", [])
            return _json_result(
                {
                    "schemaVersion": document.get("schemaVersion"),
                    "asset": document.get("asset", asset),
                    "class": document.get("class"),
                    "parent": document.get("parent"),
                    "engineVersion": document.get("engineVersion"),
                    "parseStatus": document.get("parseStatus"),
                    "defaultValues": default_values[:max_defaults],
                    "defaultValuesTruncated": len(default_values) > max_defaults,
                    "exportedObjects": exported_objects[:max_exported_objects],
                    "exportedObjectsTruncated": len(exported_objects) > max_exported_objects,
                    "properties": properties[:max_properties],
                    "propertiesTruncated": len(properties) > max_properties,
                    "functions": [
                        {
                            "name": function.get("name"),
                            "calls": function.get("calls", []),
                            "reads": function.get("reads", []),
                            "writes": function.get("writes", []),
                            "branches": function.get("branches", 0),
                            "diagnostics": function.get("diagnostics", []),
                        }
                        for function in functions[:max_functions]
                    ],
                    "functionsTruncated": len(functions) > max_functions,
                    "callEdges": document.get("callEdges", []),
                    "diagnostics": document.get("diagnostics", []),
                }
            )

        if name == "list_exported_objects":
            return _json_result(
                self.catalog.list_exported_objects(
                    str(arguments.get("asset", "")),
                    str(arguments.get("className", "")),
                    str(arguments.get("name", "")),
                    str(arguments.get("outer", "")),
                    str(arguments.get("path", "")),
                    str(arguments.get("propertyName", "")),
                    bounded_limit(arguments.get("limit")),
                    max(0, int(arguments.get("offset", 0))),
                )
            )

        if name == "get_exported_object":
            return _json_result(
                self.catalog.get_exported_object(
                    str(arguments.get("asset", "")),
                    str(arguments.get("path", "")),
                    bounded_limit(arguments.get("maxProperties", 50)),
                )
            )

        if name == "find_exported_objects":
            return _json_result(
                self.catalog.find_exported_objects(
                    str(arguments.get("query", "")),
                    str(arguments.get("className", "")),
                    str(arguments.get("classExact", "")),
                    str(arguments.get("assetPrefix", "")),
                    str(arguments.get("propertyName", "")),
                    str(arguments.get("propertyValue", "")),
                    bounded_limit(arguments.get("limit")),
                    max(0, int(arguments.get("offset", 0))),
                )
            )

        if name == "get_function_summary":
            asset = str(arguments.get("asset", ""))
            function_name = str(arguments.get("function", ""))
            document = self.catalog.load_asset(asset)
            function = next(
                (item for item in document.get("functions", []) if item.get("name", "").casefold() == function_name.casefold()),
                None,
            )
            if function is None:
                raise KeyError(f"Function is not indexed on asset: {function_name}")
            result = dict(function)
            if not bool(arguments.get("includeBlocks", False)):
                result.pop("blocks", None)
            else:
                result["blocks"] = result.get("blocks", [])[: bounded_limit(arguments.get("maxBlocks"))]
            result["asset"] = document.get("asset", asset)
            return _json_result(result)

        if name == "get_control_flow":
            asset = str(arguments.get("asset", ""))
            function_name = str(arguments.get("function", ""))
            document = self.catalog.load_asset(asset)
            function = next(
                (item for item in document.get("functions", []) if item.get("name", "").casefold() == function_name.casefold()),
                None,
            )
            if function is None:
                raise KeyError(f"Function is not indexed on asset: {function_name}")
            blocks = function.get("blocks", [])
            limit = bounded_limit(arguments.get("maxBlocks"))
            return _json_result(
                {
                    "asset": document.get("asset", asset),
                    "function": function.get("name"),
                    "blocks": blocks[:limit],
                    "truncated": len(blocks) > limit,
                    "diagnostics": function.get("diagnostics", []),
                }
            )

        if name == "get_call_graph":
            asset = str(arguments.get("asset", ""))
            root_name = str(arguments.get("function", ""))
            direction = str(arguments.get("direction", "callees"))
            max_depth = max(0, min(int(arguments.get("maxDepth", 2)), 5))
            max_nodes = bounded_limit(arguments.get("maxNodes", 30))
            document = self.catalog.load_asset(asset)
            functions = {item.get("name", ""): item for item in document.get("functions", [])}
            root = next((name for name in functions if name.casefold() == root_name.casefold()), None)
            if root is None:
                raise KeyError(f"Function is not indexed on asset: {root_name}")
            outgoing = {name: [call for call in item.get("calls", []) if call in functions] for name, item in functions.items()}
            incoming = {name: [] for name in functions}
            for source, targets in outgoing.items():
                for target in targets:
                    incoming[target].append(source)
            seen = {root}
            frontier = [(root, 0)]
            edges = []
            while frontier and len(seen) <= max_nodes:
                current, depth = frontier.pop(0)
                if depth >= max_depth:
                    continue
                targets = []
                if direction in ("callees", "both"):
                    targets.extend((target, "callee") for target in outgoing[current])
                if direction in ("callers", "both"):
                    targets.extend((target, "caller") for target in incoming[current])
                for target, kind in targets:
                    edges.append({"from": current, "to": target, "kind": kind})
                    if target not in seen and len(seen) < max_nodes:
                        seen.add(target)
                        frontier.append((target, depth + 1))
            return _json_result(
                {
                    "asset": document.get("asset", asset),
                    "root": root,
                    "direction": direction,
                    "nodes": sorted(seen),
                    "edges": edges,
                    "truncated": bool(frontier) or len(seen) >= max_nodes,
                }
            )

        if name == "trace_behavior":
            query = str(arguments.get("query", ""))
            candidates: list[dict[str, Any]] = []
            requested_asset = arguments.get("asset")
            if requested_asset:
                entry = self.catalog.get_entry(str(requested_asset))
                if entry:
                    candidates.append(self.catalog._manifest_summary(entry))
            else:
                candidates = self.catalog.find_assets(query, bounded_limit(arguments.get("maxNodes", 20)))["results"]

            evidence = []
            requested_function = arguments.get("function")
            for candidate in candidates:
                asset = candidate.get("asset")
                functions = candidate.get("functions", [])
                function = str(requested_function or (functions[0] if functions else ""))
                if not function:
                    continue
                try:
                    document = self.catalog.load_asset(asset)
                    selected = next(
                        (item for item in document.get("functions", []) if item.get("name", "").casefold() == function.casefold()),
                        None,
                    )
                    if selected:
                        graph = self.call_tool(
                            "get_call_graph",
                            {
                                "asset": asset,
                                "function": selected.get("name"),
                                "maxDepth": arguments.get("maxDepth", 2),
                                "maxNodes": arguments.get("maxNodes", 20),
                            },
                        )["structuredContent"]
                        evidence.append(
                            {
                                "asset": asset,
                                "class": document.get("class"),
                                "function": selected.get("name"),
                                "calls": selected.get("calls", []),
                                "reads": selected.get("reads", []),
                                "writes": selected.get("writes", []),
                                "graph": graph,
                                "diagnostics": selected.get("diagnostics", []),
                            }
                        )
                except (KeyError, FileNotFoundError, ValueError):
                    continue
            return _json_result({"query": query, "candidates": candidates, "evidence": evidence})

        if name == "compare_assets":
            left_name = str(arguments.get("left", ""))
            right_name = str(arguments.get("right", ""))
            left = self.catalog.load_asset(left_name)
            right = self.catalog.load_asset(right_name)

            def names(items: list[dict[str, Any]]) -> set[str]:
                return {str(item.get("name", "")) for item in items}

            left_functions = names(left.get("functions", []))
            right_functions = names(right.get("functions", []))
            left_properties = names(left.get("properties", []))
            right_properties = names(right.get("properties", []))
            return _json_result(
                {
                    "left": left.get("asset", left_name),
                    "right": right.get("asset", right_name),
                    "classes": {"left": left.get("class"), "right": right.get("class"), "same": left.get("class") == right.get("class")},
                    "functions": {
                        "shared": sorted(left_functions & right_functions),
                        "leftOnly": sorted(left_functions - right_functions),
                        "rightOnly": sorted(right_functions - left_functions),
                    },
                    "properties": {
                        "shared": sorted(left_properties & right_properties),
                        "leftOnly": sorted(left_properties - right_properties),
                        "rightOnly": sorted(right_properties - left_properties),
                    },
                    "parseStatus": {"left": left.get("parseStatus"), "right": right.get("parseStatus")},
                }
            )

        if name == "get_implementation_spec":
            asset = str(arguments.get("asset", ""))
            function_name = str(arguments.get("function", ""))
            document = self.catalog.load_asset(asset)
            function = next(
                (item for item in document.get("functions", []) if item.get("name", "").casefold() == function_name.casefold()),
                None,
            )
            if function is None:
                raise KeyError(f"Function is not indexed on asset: {function_name}")
            graph = self.call_tool(
                "get_call_graph",
                {"asset": asset, "function": function.get("name"), "maxDepth": 2, "maxNodes": 30},
            )["structuredContent"]
            blocks = function.get("blocks", [])
            max_blocks = bounded_limit(arguments.get("maxBlocks", 20))
            return _json_result(
                {
                    "source": {"asset": document.get("asset", asset), "function": function.get("name")},
                    "targetActor": arguments.get("targetActor"),
                    "implementation": {
                        "lifecycleEntry": function.get("name"),
                        "requiredStateReads": function.get("reads", []),
                        "requiredStateWrites": function.get("writes", []),
                        "callsToRecreateOrAdapt": function.get("calls", []),
                        "branchCount": function.get("branches", 0),
                        "controlFlow": blocks[:max_blocks],
                        "controlFlowTruncated": len(blocks) > max_blocks,
                        "callGraph": graph,
                    },
                    "diagnostics": function.get("diagnostics", []) + document.get("diagnostics", []),
                    "verification": [
                        "Confirm imported calls exist or provide replacements.",
                        "Validate read/write state names against the target actor.",
                        "Test branch behavior using equivalent gameplay inputs.",
                    ],
                }
            )

        if name == "find_references":
            return _json_result(self.catalog.find_references(str(arguments.get("query", "")), bounded_limit(arguments.get("limit"))))

        if name == "get_catalog_stats":
            return _json_result(
                {
                    "catalog": str(self.catalog.root),
                    "index": str(self.catalog.index_path),
                    **self.catalog.index_stats(),
                    "recentCalls": [
                        {"tool": tool_name, "ms": round(elapsed_ms, 1)}
                        for tool_name, elapsed_ms in self._recent_calls
                    ],
                }
            )

        if name == "refresh_catalog":
            self.catalog.refresh()
            return _json_result({"refreshed": True, **self.catalog.index_stats()})

        raise KeyError(f"Unknown tool: {name}")

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get("method")
        request_id = request.get("id")

        if method == "notifications/initialized" or method == "notifications/cancelled":
            return None
        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": request.get("params", {}).get("protocolVersion", "2024-11-05"),
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "kismet-analyzer-mcp", "version": "0.1.0"},
                    "instructions": "Read-only semantic asset search over the configured kismet-analyzer output tree.",
                },
            }
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
        if method == "tools/call":
            params = request.get("params", {})
            try:
                result = self.call_tool(str(params.get("name", "")), params.get("arguments", {}))
                return {"jsonrpc": "2.0", "id": request_id, "result": result}
            except (KeyError, FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
                return {"jsonrpc": "2.0", "id": request_id, "result": _error_result(str(exc))}

        if request_id is None:
            return None
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }


def run() -> None:
    logging.basicConfig(stream=sys.stderr, level=os.environ.get("KISMET_MCP_LOG_LEVEL", "INFO"))
    try:
        server = McpServer(Catalog(default_catalog_root()))
    except Exception as exc:
        LOGGER.error("Unable to load catalog: %s", exc)
        raise

    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            response = server.handle(request)
            if response is not None:
                sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
                sys.stdout.flush()
        except json.JSONDecodeError as exc:
            LOGGER.warning("Invalid JSON-RPC input: %s", exc)
        except Exception:
            LOGGER.exception("Unhandled MCP request failure")


if __name__ == "__main__":
    run()

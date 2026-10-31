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
from concurrent.futures import ThreadPoolExecutor
from itertools import islice
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger("kismet-analyzer-mcp")
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
# Bump when the index schema changes; a mismatched index is wiped and rebuilt.
# 2: adds the precomputed lowercase `search` column for find_assets.
# 3: adds the `parent` column + index for class-hierarchy queries.
SCHEMA_VERSION = "3"


def _json_result(value: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(value, indent=2)}], "structuredContent": value}


def _error_result(message: str) -> dict[str, Any]:
    return {
        "isError": True,
        "content": [{"type": "text", "text": message}],
    }


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS assets (
    asset TEXT PRIMARY KEY, class TEXT, parent TEXT, output TEXT, parse_status TEXT,
    reason TEXT, functions TEXT NOT NULL, properties TEXT NOT NULL,
    default_values TEXT NOT NULL DEFAULT '[]',
    search TEXT NOT NULL DEFAULT ''
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
CREATE INDEX IF NOT EXISTS references_target ON references_index(target);
CREATE INDEX IF NOT EXISTS assets_parent ON assets(parent);
"""


class Catalog:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.manifest_path = self.root / "manifest.json"
        configured_index = os.environ.get("KISMET_MCP_INDEX")
        self.index_path = Path(configured_index).expanduser() if configured_index else self.root / ".mcp-index.sqlite3"
        self.entries: list[dict[str, Any]] = []
        self.by_asset: dict[str, dict[str, Any]] = {}
        self.refresh()

    @staticmethod
    def workspace_root() -> Path:
        return Path(__file__).resolve().parent.parent

    def refresh(self) -> None:
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
                functions_blob = json.dumps(entry.get("functions", [f.get("name") for f in functions]))
                properties_blob = json.dumps(properties)
                defaults_blob = json.dumps(default_values)
                class_name = entry.get("class") or document.get("class") or ""
                parent_name = document.get("parent")
                # Precomputed lowercase search text: find_assets scans this
                # column directly instead of re-concatenating and lowercasing
                # every row's full JSON text per query. Parent is included so
                # searching a parent class name finds its subclasses.
                search_blob = (
                    f"{asset} {class_name} {parent_name or ''} "
                    f"{functions_blob} {properties_blob} {defaults_blob}"
                ).lower()
                db.execute(
                    "INSERT INTO assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
        with sqlite3.connect(self.index_path) as db:
            # One text scan per call: collect the matching keys, then
            # detail-fetch only the requested page by primary key. A separate
            # COUNT(*) would rescan the full search text a second time.
            descendants_truncated = False
            if include_descendants:
                # Expand every matched class's subtree in the same pass: the
                # recursive step resolves children through the parent index.
                # Fetch one extra row to detect truncation.
                expanded = db.execute(
                    """
                    WITH RECURSIVE matched(asset, class) AS (
                        SELECT asset, class FROM assets WHERE search LIKE ? AND class IS NOT NULL
                    ),
                    tree(asset, class) AS (
                        SELECT asset, class FROM matched
                        UNION
                        SELECT a.asset, a.class FROM assets a JOIN tree t ON a.parent = t.class
                    )
                    SELECT asset FROM tree ORDER BY asset LIMIT ?
                    """,
                    (needle, max_nodes + 1),
                ).fetchall()
                descendants_truncated = len(expanded) > max_nodes
                keys = [row[0] for row in expanded[:max_nodes]]
            else:
                keys = [
                    row[0]
                    for row in db.execute(
                        "SELECT asset FROM assets WHERE search LIKE ? ORDER BY asset", (needle,)
                    ).fetchall()
                ]
            count = len(keys)
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
                "hasDefaultValues": bool(json.loads(default_values)),
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
        "description": "Get concise semantic metadata, properties, functions, and diagnostics for one asset.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "asset": {"type": "string", "description": "Indexed asset path."},
                "maxProperties": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": 50},
                "maxFunctions": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": 50},
                "maxDefaults": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": 50},
            },
            "required": ["asset"],
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

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
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
            properties = document.get("properties", [])
            functions = document.get("functions", [])
            default_values = document.get("defaultValues", [])
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
            return _json_result({"catalog": str(self.catalog.root), "index": str(self.catalog.index_path), **self.catalog.index_stats()})

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
    logging.basicConfig(stream=sys.stderr, level=os.environ.get("KISMET_MCP_LOG_LEVEL", "WARNING"))
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

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
import sys
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger("kismet-analyzer-mcp")
DEFAULT_LIMIT = 20
MAX_LIMIT = 100


def _json_result(value: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(value, indent=2)}], "structuredContent": value}


def _error_result(message: str) -> dict[str, Any]:
    return {
        "isError": True,
        "content": [{"type": "text", "text": message}],
    }


class Catalog:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.manifest_path = self.root / "manifest.json"
        self.entries: list[dict[str, Any]] = []
        self.by_asset: dict[str, dict[str, Any]] = {}
        self.refresh()

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

    @staticmethod
    def _normalize(asset: str) -> str:
        return asset.replace("\\", "/").lstrip("/").casefold()

    def find_assets(self, query: str, limit: int, offset: int = 0) -> dict[str, Any]:
        needle = query.casefold().strip()
        matches = []
        for entry in self.entries:
            haystack = " ".join(
                [
                    str(entry.get("asset", "")),
                    str(entry.get("class", "")),
                    " ".join(entry.get("functions", [])),
                ]
            ).casefold()
            if not needle or needle in haystack:
                matches.append(self._manifest_summary(entry))

        page = matches[offset : offset + limit]
        next_offset = offset + limit if offset + limit < len(matches) else None
        return {
            "query": query,
            "results": page,
            "count": len(matches),
            "nextOffset": next_offset,
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
        "description": "Find indexed assets by path, class, or function name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Text to search for."},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": DEFAULT_LIMIT},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_asset_summary",
        "description": "Get concise semantic metadata, properties, functions, and diagnostics for one asset.",
        "inputSchema": {
            "type": "object",
            "properties": {"asset": {"type": "string", "description": "Indexed asset path."}},
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
]


class McpServer:
    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "find_assets":
            return _json_result(
                self.catalog.find_assets(
                    str(arguments.get("query", "")),
                    bounded_limit(arguments.get("limit")),
                    max(0, int(arguments.get("offset", 0))),
                )
            )

        if name == "get_asset_summary":
            asset = str(arguments.get("asset", ""))
            document = self.catalog.load_asset(asset)
            return _json_result(
                {
                    "schemaVersion": document.get("schemaVersion"),
                    "asset": document.get("asset", asset),
                    "class": document.get("class"),
                    "parent": document.get("parent"),
                    "engineVersion": document.get("engineVersion"),
                    "parseStatus": document.get("parseStatus"),
                    "properties": document.get("properties", []),
                    "functions": [
                        {
                            "name": function.get("name"),
                            "calls": function.get("calls", []),
                            "reads": function.get("reads", []),
                            "writes": function.get("writes", []),
                            "branches": function.get("branches", 0),
                            "diagnostics": function.get("diagnostics", []),
                        }
                        for function in document.get("functions", [])
                    ],
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

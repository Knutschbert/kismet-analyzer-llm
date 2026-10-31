"""Integration tests for the MCP server over the checked-in fixture tree.

`StdioIntegrationTests` launches the real server process and speaks JSON-RPC
over stdio, exercising the full transport. `FixtureCatalogTests` runs the
server in-process against the static fixture to cover the workflow tools.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mcp_server.server import Catalog, McpServer

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_TREE = REPO_ROOT / "mcp_server" / "fixtures" / "tree"
TRAP_DOOR = "Content/Fixtures/TrapDoor.uasset"
PARTIAL = "Content/Fixtures/TrapDoorPartial.uasset"
EMPTY = "Content/Fixtures/PlaceholderTexture.uasset"


class StdioIntegrationTests(unittest.TestCase):
    """Run `python -m mcp_server.server` as a subprocess and exchange JSON-RPC."""

    def setUp(self):
        self.index = Path(tempfile.mkdtemp()) / "index.sqlite3"
        self.env = os.environ.copy()
        self.env["KISMET_LLM_DATA"] = str(FIXTURE_TREE)
        self.env["KISMET_MCP_INDEX"] = str(self.index)

    def _exchange(self, requests):
        payload = "".join(json.dumps(request) + "\n" for request in requests)
        process = subprocess.run(
            [sys.executable, "-m", "mcp_server.server"],
            input=payload,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env=self.env,
            timeout=120,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        lines = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
        return {response.get("id"): response for response in lines}

    def test_stdio_lifecycle_and_tool_calls(self):
        responses = self._exchange(
            [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "find_assets", "arguments": {"query": "TrapDoor"}},
                },
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "get_function_summary",
                        "arguments": {"asset": TRAP_DOOR, "function": "Open", "includeBlocks": True},
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {"name": "trace_behavior", "arguments": {"query": "TrapDoor", "maxNodes": 5}},
                },
            ]
        )

        # The notification produces no response line; every id is answered.
        self.assertEqual(sorted(responses), [1, 2, 3, 4, 5])

        initialize = responses[1]["result"]
        self.assertEqual(initialize["protocolVersion"], "2024-11-05")
        self.assertEqual(initialize["serverInfo"]["name"], "kismet-analyzer-mcp")
        self.assertIn("tools", initialize["capabilities"])

        tool_names = {tool["name"] for tool in responses[2]["result"]["tools"]}
        self.assertIn("get_implementation_spec", tool_names)
        self.assertIn("find_references", tool_names)
        self.assertEqual(len(tool_names), 11)

        found = responses[3]["result"]["structuredContent"]
        assets = [result["asset"] for result in found["results"]]
        self.assertIn(TRAP_DOOR, assets)
        self.assertGreaterEqual(found["count"], 2)

        function = responses[4]["result"]["structuredContent"]
        self.assertEqual(function["name"], "Open")
        self.assertEqual(len(function["blocks"]), 1)

        behavior = responses[5]["result"]["structuredContent"]
        self.assertTrue(behavior["evidence"])
        self.assertEqual(behavior["evidence"][0]["function"], "BeginPlay")

    def test_tool_errors_and_unknown_methods(self):
        responses = self._exchange(
            [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "no_such_tool", "arguments": {}},
                },
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "get_asset_summary", "arguments": {"asset": EMPTY}},
                },
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "resources/list",
                },
            ]
        )

        unknown_tool = responses[2]["result"]
        self.assertTrue(unknown_tool["isError"])
        self.assertIn("Unknown tool", unknown_tool["content"][0]["text"])

        empty_asset = responses[3]["result"]["structuredContent"]
        self.assertEqual(empty_asset["parseStatus"], "empty")
        self.assertEqual(empty_asset["functions"], [])

        method_error = responses[4]
        self.assertEqual(method_error["error"]["code"], -32601)

    def test_ping_is_answered(self):
        responses = self._exchange([{"jsonrpc": "2.0", "id": 7, "method": "ping"}])
        self.assertEqual(responses[7]["result"], {})


class FixtureCatalogTests(unittest.TestCase):
    """In-process coverage for workflow tools over the static fixture tree."""

    def setUp(self):
        index = Path(tempfile.mkdtemp()) / "index.sqlite3"
        with mock.patch.dict(os.environ, {"KISMET_MCP_INDEX": str(index)}):
            self.server = McpServer(Catalog(FIXTURE_TREE))

    def test_control_flow_and_pagination(self):
        flow = self.server.call_tool(
            "get_control_flow", {"asset": TRAP_DOOR, "function": "BeginPlay", "maxBlocks": 1}
        )
        self.assertEqual(len(flow["structuredContent"]["blocks"]), 1)
        self.assertTrue(flow["structuredContent"]["truncated"])

        page = self.server.call_tool("find_assets", {"query": "", "limit": 1, "offset": 0})
        self.assertEqual(len(page["structuredContent"]["results"]), 1)
        self.assertEqual(page["structuredContent"]["nextOffset"], 1)
        self.assertEqual(page["structuredContent"]["count"], 3)

    def test_compare_assets_and_partial_diagnostics(self):
        comparison = self.server.call_tool(
            "compare_assets", {"left": TRAP_DOOR, "right": PARTIAL}
        )
        self.assertFalse(comparison["structuredContent"]["classes"]["same"])
        self.assertEqual(
            comparison["structuredContent"]["functions"]["leftOnly"], ["BeginPlay", "Open"]
        )

        partial = self.server.call_tool("get_asset_summary", {"asset": PARTIAL})
        self.assertEqual(partial["structuredContent"]["parseStatus"], "partial")
        self.assertTrue(partial["structuredContent"]["diagnostics"])

    def test_implementation_spec_contains_evidence(self):
        spec = self.server.call_tool(
            "get_implementation_spec",
            {"asset": TRAP_DOOR, "function": "Open", "targetActor": "NewTrapDoor_C"},
        )
        implementation = spec["structuredContent"]["implementation"]
        self.assertEqual(implementation["lifecycleEntry"], "Open")
        self.assertEqual(implementation["requiredStateWrites"], ["DoorState"])
        self.assertIn("Open", implementation["callGraph"]["nodes"])
        self.assertTrue(spec["structuredContent"]["verification"])

    def test_catalog_stats_and_refresh(self):
        stats = self.server.call_tool("get_catalog_stats", {})
        self.assertEqual(stats["structuredContent"]["assets"], 3)
        self.assertEqual(stats["structuredContent"]["semanticAssets"], 2)
        self.assertEqual(stats["structuredContent"]["functions"], 3)
        self.assertEqual(stats["structuredContent"]["references"], 4)

        refreshed = self.server.call_tool("refresh_catalog", {})
        self.assertTrue(refreshed["structuredContent"]["refreshed"])
        self.assertEqual(refreshed["structuredContent"]["assets"], 3)


if __name__ == "__main__":
    unittest.main()

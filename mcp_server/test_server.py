import json
import tempfile
import unittest
from pathlib import Path

from mcp_server.server import Catalog, McpServer


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "Content").mkdir()
        (self.root / "manifest.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "assets": [
                        {
                            "asset": "Content/BP_Test.uasset",
                            "output": "Content/BP_Test.llm.json",
                            "class": "BP_Test_C",
                            "functions": ["BeginPlay"],
                            "parseStatus": "complete",
                            "reason": None,
                        },
                        {
                            "asset": "Content/Texture.uasset",
                            "output": None,
                            "class": None,
                            "functions": [],
                            "parseStatus": "empty",
                            "reason": "no_semantic_content",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        (self.root / "Content/BP_Test.llm.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "asset": "Content/BP_Test.uasset",
                    "class": "BP_Test_C",
                    "functions": [
                        {
                            "name": "BeginPlay",
                            "calls": ["Initialize"],
                            "reads": ["Health"],
                            "writes": [],
                            "branches": 0,
                            "blocks": [{"id": 0, "code": ["call Initialize"], "successors": []}],
                            "diagnostics": [],
                        },
                        {
                            "name": "Initialize",
                            "calls": [],
                            "reads": [],
                            "writes": [],
                            "branches": 0,
                            "blocks": [{"id": 0, "code": ["return"], "successors": []}],
                            "diagnostics": [],
                        },
                    ],
                    "properties": [],
                    "callEdges": [{"from": "BeginPlay", "to": "Initialize"}],
                    "diagnostics": [],
                }
            ),
            encoding="utf-8",
        )
        self.server = McpServer(Catalog(self.root))

    def test_find_assets_and_empty_asset_summary(self):
        found = self.server.call_tool("find_assets", {"query": "BP_Test"})
        self.assertIn("BP_Test.uasset", found["structuredContent"]["results"][0]["asset"])

        empty = self.server.call_tool("get_asset_summary", {"asset": "Content/Texture.uasset"})
        self.assertEqual(empty["structuredContent"]["parseStatus"], "empty")

    def test_function_summary_hides_blocks_by_default(self):
        summary = self.server.call_tool(
            "get_function_summary", {"asset": "Content/BP_Test.uasset", "function": "BeginPlay"}
        )
        self.assertNotIn("blocks", summary["structuredContent"])

        detailed = self.server.call_tool(
            "get_function_summary",
            {"asset": "Content/BP_Test.uasset", "function": "BeginPlay", "includeBlocks": True},
        )
        self.assertEqual(len(detailed["structuredContent"]["blocks"]), 1)

    def test_call_graph_and_reference_search(self):
        graph = self.server.call_tool(
            "get_call_graph", {"asset": "Content/BP_Test.uasset", "function": "BeginPlay"}
        )
        self.assertIn("Initialize", graph["structuredContent"]["nodes"])
        refs = self.server.call_tool("find_references", {"query": "Health"})
        self.assertEqual(refs["structuredContent"]["results"][0]["kind"], "property_read")


if __name__ == "__main__":
    unittest.main()

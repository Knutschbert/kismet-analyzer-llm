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

    def test_class_lineage_and_descendants(self):
        root = Path(tempfile.mkdtemp())
        (root / "Content").mkdir()
        family = [
            ("Content/BaseSpawner.uasset", "BaseSpawner_C", "Actor", ["BeginPlay"]),
            ("Content/SpawnerA.uasset", "SpawnerA_C", "BaseSpawner_C", []),
            ("Content/SpawnerB.uasset", "SpawnerB_C", "BaseSpawner_C", []),
            ("Content/SpawnerA_Heavy.uasset", "SpawnerA_Heavy_C", "SpawnerA_C", []),
        ]
        manifest_assets = []
        for asset, class_name, parent, functions in family:
            manifest_assets.append(
                {
                    "asset": asset,
                    "output": asset.replace(".uasset", ".llm.json"),
                    "class": class_name,
                    "functions": functions,
                    "parseStatus": "complete",
                    "reason": None,
                }
            )
            (root / asset.replace(".uasset", ".llm.json")).write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "asset": asset,
                        "class": class_name,
                        "parent": parent,
                        "functions": [],
                        "properties": [],
                        "callEdges": [],
                        "diagnostics": [],
                    }
                ),
                encoding="utf-8",
            )
        (root / "manifest.json").write_text(
            json.dumps({"schemaVersion": 1, "assets": manifest_assets}),
            encoding="utf-8",
        )
        server = McpServer(Catalog(root))

        # Lineage by asset path: children, siblings, ancestry to a native class.
        lineage = server.call_tool("get_lineage", {"asset": "Content/SpawnerA.uasset"})["structuredContent"]
        self.assertEqual(lineage["class"], "SpawnerA_C")
        self.assertEqual(lineage["parent"], "BaseSpawner_C")
        self.assertEqual(lineage["childCount"], 1)
        self.assertEqual(lineage["children"][0]["class"], "SpawnerA_Heavy_C")
        self.assertEqual(lineage["siblingCount"], 1)
        self.assertEqual(lineage["siblings"][0]["class"], "SpawnerB_C")
        ancestry_classes = [item["class"] for item in lineage["ancestry"]]
        self.assertEqual(ancestry_classes, ["SpawnerA_C", "BaseSpawner_C", "Actor"])
        self.assertFalse(lineage["ancestry"][-1]["inCatalog"])  # Actor is not an indexed asset
        self.assertFalse(lineage["ancestryTruncated"])

        # Lineage by class name, with or without the _C suffix.
        by_class = server.call_tool("get_lineage", {"asset": "SpawnerA"})["structuredContent"]
        self.assertEqual(by_class["class"], "SpawnerA_C")
        by_class_c = server.call_tool("get_lineage", {"asset": "SpawnerA_C"})["structuredContent"]
        self.assertEqual(by_class_c["class"], "SpawnerA_C")

        # Descendant expansion: the needle only matches the root blueprint's
        # asset path, so plain search returns 1 and expanded search returns 4.
        plain = server.call_tool("find_assets", {"query": "BaseSpawner.uasset"})["structuredContent"]
        self.assertEqual(plain["count"], 1)
        expanded = server.call_tool(
            "find_assets", {"query": "BaseSpawner.uasset", "includeDescendants": True}
        )["structuredContent"]
        self.assertEqual(expanded["count"], 4)
        self.assertFalse(expanded["descendantsTruncated"])
        expanded_assets = {result["asset"] for result in expanded["results"]}
        self.assertIn("Content/SpawnerA_Heavy.uasset", expanded_assets)

        # The expansion is bounded and reports truncation.
        bounded = server.call_tool(
            "find_assets",
            {"query": "BaseSpawner.uasset", "includeDescendants": True, "maxNodes": 2},
        )["structuredContent"]
        self.assertEqual(bounded["count"], 2)
        self.assertTrue(bounded["descendantsTruncated"])


if __name__ == "__main__":
    unittest.main()

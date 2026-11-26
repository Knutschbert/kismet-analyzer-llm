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
                        {
                            "asset": "Content/V1Asset.uasset",
                            "output": "Content/V1Asset.llm.json",
                            "class": "V1Asset_C",
                            "functions": [],
                            "parseStatus": "complete",
                            "reason": None,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        (self.root / "Content/BP_Test.llm.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 2,
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
                    "exportedObjects": [
                        {
                            "name": "SpwnS4cAtkVIP1",
                            "class": "BP_FreeableVIPSpawner_C",
                            "outer": "PersistentLevel",
                            "path": "PersistentLevel.SpwnS4cAtkVIP1",
                            "properties": [
                                {
                                    "name": "SpawnWaveParams",
                                    "type": "Struct",
                                    "value": "SpawnWaveParamOverrides(SpecialSpawnSpec=WarHeroSpawn_C)",
                                },
                                {
                                    "name": "StreamingTextureData",
                                    "type": "Array",
                                    "value": "EXCLUDED_FROM_SEARCH_XYZ",
                                },
                            ],
                        }
                    ],
                    "callEdges": [{"from": "BeginPlay", "to": "Initialize"}],
                    "diagnostics": [],
                }
            ),
            encoding="utf-8",
        )
        (self.root / "Content/V1Asset.llm.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "asset": "Content/V1Asset.uasset",
                    "class": "V1Asset_C",
                    "functions": [],
                    "properties": [],
                    "callEdges": [],
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

    def test_asset_summary_returns_bounded_exported_objects(self):
        summary = self.server.call_tool(
            "get_asset_summary",
            {"asset": "Content/BP_Test.uasset", "maxExportedObjects": 1},
        )["structuredContent"]

        self.assertEqual(summary["schemaVersion"], 2)
        self.assertEqual(summary["exportedObjects"][0]["class"], "BP_FreeableVIPSpawner_C")
        self.assertFalse(summary["exportedObjectsTruncated"])

    def test_list_exported_objects_filters_by_class_and_property(self):
        listed = self.server.call_tool(
            "list_exported_objects",
            {
                "asset": "Content/BP_Test.uasset",
                "className": "FreeableVIP",
                "propertyName": "SpawnWaveParams",
            },
        )["structuredContent"]

        self.assertTrue(listed["exportedObjectsAvailable"])
        self.assertEqual(listed["total"], 1)
        self.assertEqual(listed["results"][0]["path"], "PersistentLevel.SpwnS4cAtkVIP1")
        self.assertEqual(listed["results"][0]["propertyCount"], 2)

        none = self.server.call_tool(
            "list_exported_objects",
            {"asset": "Content/BP_Test.uasset", "className": "CagedSpawner"},
        )["structuredContent"]
        self.assertEqual(none["total"], 0)

    def test_get_exported_object_by_stable_path(self):
        result = self.server.call_tool(
            "get_exported_object",
            {"asset": "Content/BP_Test.uasset", "path": "PersistentLevel.SpwnS4cAtkVIP1"},
        )["structuredContent"]

        self.assertTrue(result["found"])
        self.assertFalse(result["ambiguous"])
        self.assertEqual(result["exportedObject"]["class"], "BP_FreeableVIPSpawner_C")
        self.assertIn("WarHeroSpawn_C", result["exportedObject"]["properties"][0]["value"])

    def test_find_exported_objects_is_indexed_and_bounded(self):
        found = self.server.call_tool(
            "find_exported_objects",
            {"className": "FreeableVIPSpawner", "propertyValue": "WarHeroSpawn_C", "limit": 1},
        )["structuredContent"]

        self.assertEqual(found["total"], 1)
        self.assertEqual(found["results"][0]["asset"], "Content/BP_Test.uasset")
        self.assertEqual(found["coverage"]["assetsWithExportedObjects"], 1)
        self.assertGreaterEqual(found["coverage"]["totalAssets"], 2)

        exact = self.server.call_tool(
            "find_exported_objects", {"classExact": "BP_FreeableVIPSpawner_C"}
        )["structuredContent"]
        self.assertEqual(exact["total"], 1)
        self.assertEqual(exact["results"][0]["path"], "PersistentLevel.SpwnS4cAtkVIP1")

    def test_excluded_property_is_kept_but_not_globally_searched(self):
        text = self.server.call_tool("find_exported_objects", {"query": "EXCLUDED_FROM_SEARCH_XYZ"})["structuredContent"]
        self.assertEqual(text["total"], 0)

        explicit = self.server.call_tool(
            "find_exported_objects", {"propertyName": "StreamingTextureData"}
        )["structuredContent"]
        self.assertEqual(explicit["total"], 1)

    def test_schema_v1_exported_objects_are_unknown(self):
        listed = self.server.call_tool(
            "list_exported_objects", {"asset": "Content/V1Asset.uasset"}
        )["structuredContent"]
        self.assertFalse(listed["exportedObjectsAvailable"])
        self.assertIsNotNone(listed["coverageNote"])

        fetched = self.server.call_tool(
            "get_exported_object", {"asset": "Content/V1Asset.uasset", "path": "Missing"}
        )["structuredContent"]
        self.assertFalse(fetched["found"])

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

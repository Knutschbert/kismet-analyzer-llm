"""Benchmark harness for the kismet-analyzer MCP catalog.

Measures cold index rebuild, warm init, find_assets, and get_asset_summary
over a generated semantic tree. Numbers and analysis live in
docs/performance.md.

Usage:
    PYTHONPATH=<workspace root> python3 scripts/bench-mcp.py \
        <data root> <index path> <label> [--profile]

    --profile adds a cProfile breakdown of the cold rebuild.
"""

import cProfile
import json
import os
import pstats
import random
import sys
import time

data_root, index_path, label = sys.argv[1], sys.argv[2], sys.argv[3]
profile = len(sys.argv) > 4 and sys.argv[4] == "--profile"
os.environ["KISMET_LLM_DATA"] = data_root
os.environ["KISMET_MCP_INDEX"] = index_path

from mcp_server.server import Catalog, McpServer, default_catalog_root  # noqa: E402

# 1. Cold init = full index rebuild (fresh index file)
if profile:
    pr = cProfile.Profile()
    pr.enable()
t0 = time.perf_counter()
c = Catalog(root=default_catalog_root())
cold = time.perf_counter() - t0
if profile:
    pr.disable()
print(f"[{label}] cold init (full index rebuild): {cold:.2f}s")
if profile:
    pstats.Stats(pr).sort_stats("cumulative").print_stats(18)

s = McpServer(c)

# 2. Warm init (index mtime matches -> skip rebuild)
t0 = time.perf_counter()
Catalog(root=default_catalog_root())
print(f"[{label}] warm init (index up to date): {time.perf_counter()-t0:.2f}s")

# 3. find_assets
queries = ["Acceptance Radius", "Falmire", "Carryable", "Damage", "Fire",
           "MoveTo", "Spawn", "Door", "Weapon", "health"] * 2
t0 = time.perf_counter()
for q in queries:
    s.call_tool("find_assets", {"query": q, "limit": 20})
dt = time.perf_counter() - t0
print(f"[{label}] find_assets x{len(queries)}: {dt:.2f}s ({dt/len(queries)*1000:.1f} ms/call)")

# 4. get_asset_summary over 20 random docs
manifest = json.load(open(os.path.join(data_root, "manifest.json")))
with_output = [a["asset"] for a in manifest["assets"] if a.get("output")]
random.seed(1)
sample = random.sample(with_output, 20)
t0 = time.perf_counter()
for a in sample:
    s.call_tool("get_asset_summary", {"asset": a})
dt = time.perf_counter() - t0
print(f"[{label}] get_asset_summary x20 (mixed docs): {dt:.2f}s ({dt/20*1000:.1f} ms/call)")

# 5. Big single doc
t0 = time.perf_counter()
s.call_tool("get_asset_summary", {"asset": "Maps/PrisonBreak/TO_Falmire.umap", "maxDefaults": 50})
print(f"[{label}] get_asset_summary TO_Falmire (5.4 MB doc): {(time.perf_counter()-t0)*1000:.0f} ms")

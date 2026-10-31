# MCP catalog performance

Measured performance of the MCP server over the 52k-asset TBL semantic tree
(7,497 `.llm.json` docs, 835 MB) on the WSL2 workstation, with the catalog on a
Windows drive mounted over 9p (`/mnt/p`) versus the WSL-native filesystem
(ext4). All numbers from `scripts/bench-mcp.py` (see "Reproducing" below) and
targeted SQL timings; JSON parsing itself was never the bottleneck.

## Results

| Configuration | Index rebuild (cold) | Warm init | `find_assets` | `get_asset_summary` (20 docs) | `TO_Falmire` (5.4 MB doc) |
| --- | --- | --- | --- | --- | --- |
| P: data + P: index (9p) | 150.6 s | 0.17 s | **19.2 s/call** | 32 ms | 70 ms |
| P: data + ext4 index | 130.5 s | 0.14 s | 0.56 s/call | 28 ms | 62 ms |
| ext4 data + ext4 index (before query fixes) | 8.7 s | 0.08 s | 0.53 s/call | 0.4 ms | 23 ms |
| ext4 + all fixes (shipped) | **6.6 s** | 0.08 s | **0.22–0.6 s/call** | **0.4 ms** | 22 ms |

`find_references` is ~90 ms regardless (short target strings).

## Root causes (baseline profile, cProfile of the 150.6 s rebuild)

| Cost | Time | Cause |
| --- | --- | --- |
| `Path.resolve()` in `_load_output` | 71.6 s | 65,661 `lstat` calls over 9p — resolve() walks every path component of every doc |
| SQLite row inserts | 35.9 s | 296,507 `execute` calls with 9p journal I/O |
| Doc reads | 22 s | `stat` + `open` + `read` per doc over 9p |
| `json.loads` | 2.4 s | the actual parsing — 1.6% of the rebuild |

`find_assets` at 19.2 s/call was per-call `sqlite3.connect` + journal I/O on
the 9p index file, not query cost: with the index on ext4 the same query
dropped to ~0.55 s immediately.

The remaining ~0.55 s on ext4 was query construction, not I/O: the old WHERE
clause re-concatenated and lowercased every row's full JSON text (~520 MB) per
query, and a separate `COUNT(*)` scanned it a second time.

## Fixes (index schema v2)

1. **ext4 data location** — `scripts/sync-tree-wsl.sh` copies the generated
   tree to `~/kismet-llm-data/`; the SQLite index defaults to inside the data
   root, so it lands on ext4 too.
2. **Precomputed `search` column** — lowercase
   `asset + class + functions + properties + default_values` built once at
   rebuild time (+1.6 s); `find_assets` scans it directly.
3. **Single-scan `find_assets`** — one scan collects matching keys, then the
   requested page is detail-fetched by primary key. The old separate `COUNT(*)`
   cost a second 171 ms full scan per call.
4. **normpath containment check** in `_load_output` instead of `resolve()` —
   removes the lstat storm (the generated tree has no symlinks).
5. **`PRAGMA synchronous=OFF`** — the index is a derived cache.

## Remaining floor and needle dependence

The residual `find_assets` cost is the `LIKE '%needle%'` substring scan over
the 521 MB search text. It is needle-dependent:

- Needles matching early in a row's blob (asset, class, function names come
  first) exit rows quickly: ~0.2–0.3 s/call (`"Fire"` 215 ms, `"Damage"`
  241 ms, `"Falmire"` 264 ms).
- Needles that only appear deep in `defaultValues` JSON (the end of the blob)
  scan every row fully: ~0.5–0.6 s/call (`"Acceptance Radius"` 516–604 ms).

An FTS5 index was considered and rejected: token-based matching breaks
substring semantics that the catalog relies on (e.g. `"carryable_2hloot_c"`
tokenizes into three tokens and would no longer match). The index file roughly
doubles (~79 → ~150 MB) to hold the lowercase search text.

## Reproducing

```bash
# 1. Sync the tree off the Windows drive (or point KISMET_LLM_DATA anywhere)
scripts/sync-tree-wsl.sh TBL

# 2. Run the harness: data root, index path, label, optional --profile
PYTHONPATH=. python3 scripts/bench-mcp.py \
    ~/kismet-llm-data/TBL /tmp/bench-idx.sqlite3 "ext4 + fixes" --profile
```

The harness measures a cold init (full index rebuild), warm init, 20
`find_assets` calls over a fixed query set, 20 `get_asset_summary` calls over
random docs, and one 5.4 MB summary; `--profile` adds a cProfile breakdown of
the rebuild.

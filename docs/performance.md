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
| ext4 + all fixes (v3) | **6.6 s** | 0.08 s | **0.22–0.6 s/call** | **0.4 ms** | 22 ms |
| ext4 + scan trio (v4, shipped) | 9.0 s | 0.06 s | **50 ms/call avg** (70–116 cold, ~0.4 ms repeat) | 0.4 ms | 25 ms |

The v4 rebuild is ~1–2 s slower than v3 (the flattened blob is built with an
extra pass over properties/defaults); every `find_assets` call pays that back
many times over.

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

> Superseded by the v4 scan trio below: cold scans are now 70–116 ms and
> repeats ~0.4 ms, without changing match semantics.

## Hierarchy (index schema v3)

Schema v3 adds the `parent` column (+ index) for class-hierarchy queries.
Measured on the real tree:

- v3 rebuild: 7.9 s on ext4 (the parent column + index cost ~1.2 s over v2's
  6.6 s).
- `get_lineage`: 39–42 ms per call — full ancestry chain (e.g.
  `Carryable_Fish_2Hand_C → BaseCarryable_2hLoot_C → BaseCarryable_C →
  BaseInventoryItem_C → InventoryItem [not in catalog]`), children, and
  siblings.
- `find_assets` with `includeDescendants`: 242–333 ms per call, bounded by
  `maxNodes` (a fuzzy `"Carryable"` match expands 896 plain hits into 1,021
  nodes).

## Scan speed-up trio (index schema v4)

Schema v4 targets the `LIKE '%needle%'` scan — the remaining floor above —
without changing match semantics. Measured on the ext4 production tree
(`~/kismet-llm-data/TBL`, 52,022 rows):

| Metric | v3 (before) | v4 (after) |
| --- | --- | --- |
| Search text volume | 521 MB | 418.6 MB |
| `find_assets` cold scan | 220–600 ms (needle-dependent) | 70–116 ms |
| `find_assets` cached repeat | 220–600 ms | **~0.4 ms** |
| `find_assets` with `includeDescendants` | 242–333 ms | 87 ms |
| Official bench (10 cold + 10 cached calls) | — | 50.1 ms/call |

The fixes, in order of measured impact:

1. **Byte-balanced process-pool scan** — the scan is sharded across 8 worker
   processes, with rowid ranges balanced by search-text BYTES (cached per
   index), and pages read through `PRAGMA mmap_size` (the per-connection page
   cache copy alone was ~1.5x of a full scan: 294 → 190 ms).
2. **`hasDefaultValues` without `json.loads`** — the page builder parsed
   multi-megabyte `default_values` blobs per returned row just to compute a
   boolean; Falmire-scale pages cost 39–230 ms on top of the scan. Now a
   string compare against `'[]'`.
3. **Scan cache (LRU)** — needle → keys, 64 entries, cleared on refresh.
   Agents retry query variations; repeats are ~0.4 ms.
4. **Flattened search blob (the schema bump)** — properties/defaults are
   stored as flattened `name value` text instead of raw JSON; punctuation and
   repeated structural keys added scan volume without matchable content
   (521 → 418.6 MB, ~1.24x fewer bytes per scan).

### Debugging notes (worth recording)

- **Threads cannot parallelize this scan.** Four threads over disjoint rowid
  ranges measured the same wall time as running them sequentially (304 vs
  311 ms). The GIL is released per `sqlite3_step`, but the LIKE work
  serializes in practice; worker processes were tried next.
- **Count-balanced shards don't help either.** One 52 MB-rowid shard took
  246 ms while the full 419 MB single-process scan took 190 ms: the search
  text is heavily skewed — map docs cluster megabytes of defaults into a few
  rowid ranges — so count-based shards left one worker holding most of the
  bytes and the pool just waited on it. Balancing by `LENGTH(search)`
  brought every shard to 50–56 MB and the pool scaled accordingly.
- **Red herrings:** the query plan was already optimal (`SEARCH assets USING
  INTEGER PRIMARY KEY (rowid>? AND rowid<?)`), and the RAM footprint was not
  the constraint (13 Gi available).

### Timing visibility

- `get_catalog_stats` reports `recentCalls` — the last 25 tool calls with
  durations — so "how long did the latest calls take" is answerable by asking
  the server directly, with no log capture needed.
- Every tool call also logs `tool <name> -> <ms>` to stderr at INFO
  (`KISMET_MCP_LOG_LEVEL`, default INFO). These lines only appear when the
  logging config is applied (the `run()` entry point does it); direct module
  imports need their own `basicConfig`.

## Rebuild parallelization

The rebuild loop loads docs through a chunked `ThreadPoolExecutor`. The
reasoning, from the profiles:

- File reads release the GIL, so overlapping requests hides per-request
  latency — the dominant cost on 9p.
- `json.loads`/`json.dumps` are C code that holds the GIL, so on ext4 the
  parse phase cannot be threaded.

Measured cold rebuild:

| Filesystem | Single-threaded | Threaded ×8 |
| --- | --- | --- |
| 9p (`/mnt/p`) | 25.9 s | **7.9 s** (3.3×) |
| ext4 | 6.7–7.9 s | 6.6–7.3 s (neutral) |

Threading makes a 9p rebuild as fast as an ext4 one; on ext4 it is neutral
within noise. The chunked loader also bounds peak memory (~256 parsed docs at
a time instead of all 7,497).

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

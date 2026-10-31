# MCP design

MCP should provide query-oriented access to the asset graph rather than return
whole assets by default. The protocol is the retrieval interface; the compact
semantic JSON described in [`schema.md`](schema.md) is the response format.

## Implemented tools

All retrieval tools below are implemented in `mcp_server/server.py`:

```text
find_assets(query, limit, offset)
get_asset_summary(asset, maxProperties?, maxFunctions?)
get_function_summary(asset, function, includeBlocks?, maxBlocks?)
get_call_graph(asset, function, direction?, maxDepth?, maxNodes?)
get_control_flow(asset, function, maxBlocks?)
find_references(query, limit)
get_catalog_stats()
refresh_catalog()
trace_behavior(query, asset?, function?, maxDepth?, maxNodes?)
compare_assets(left, right)
get_implementation_spec(asset, function, targetActor?, maxBlocks?)
```

Every tool returns a bounded `structuredContent` payload with a text twin and
explicit `truncated`/`nextOffset` flags. `get_implementation_spec` is the
evidence-backed workflow for recreating a legacy function in a new actor;
`trace_behavior` is its discovery counterpart for locating legacy behavior.

## Catalog and index

The server loads `manifest.json` from the catalog root (`KISMET_LLM_DATA`) and
maintains a SQLite index beside it (`.mcp-index.sqlite3`, override with
`KISMET_MCP_INDEX`). The index is rebuilt automatically when the manifest
mtime changes and stores:

- `assets`: one row per manifest entry, including skipped-empty accounting.
- `functions`: per-function calls, reads, writes, branches, diagnostics.
- `references_index`: function-call and property read/write edges.

This keeps repeated questions from re-reading semantic documents, and
`find_assets`/`find_references` query the index instead of scanning files.
Empty assets are indexed for discovery but have no output document.

## Default values

Assets that carry no Blueprint graph (for example utility AI behaviors) hold
their meaningful data in the CDO rather than kismet bytecode. `gen-llm-tree
--defaults` emits CDO defaults into each document's `defaultValues` array, so
such property-only assets become discoverable and queryable (`parseStatus:
"complete"`) instead of being dropped as empty. See `schema.md` for the field
shape. Existing trees are unchanged unless the flag is passed.

### `get_asset_summary`

Return the Tier 0 structure: identity, parent, properties, function summaries,
and direct call edges. This should be the default starting point.

### `get_call_graph`

Return a bounded neighborhood around a function. Support both directions:

- `direction: "callees"` for what the function invokes
- `direction: "callers"` for what invokes it
- `direction: "both"` for a small local graph

Avoid returning an unbounded transitive closure. Use `depth` and `limit`.

### `get_control_flow`

Return only the requested function's selected blocks. Include successor
conditions and compact pseudo-code, not the full serialized expression object.

### `get_asset_detail` (planned)

Use for filtered properties, imports, exports, strings, raw parse failures, or
the generated `.txt` CFG. This is an escape hatch, not the normal path. It is
not implemented yet; until then, `get_asset_summary` returns the Tier 0
property list and raw forensic JSON remains a separate concern.

## Context minimization

1. Make every result bounded by default.
2. Return names instead of repeated nested object definitions.
3. Do not include raw `$type` fields in normal responses.
4. Do not include Base64 data unless explicitly requested.
5. Do not include DOT, SVG, or HTML in model responses.
6. Do not return both semantic JSON and an equivalent full text dump.
7. Include a short `truncated`/`next_cursor` field when results are limited.
8. Keep evidence close to the claim: include only the blocks supporting the
   requested behavior.
9. Cache and index the graph server-side so repeated questions do not require
   repeated full-asset serialization.

## Small versus large models

Use the same schema for both. Change the retrieval budget, not the data model:

- Smaller models: Tier 0 or Tier 1, one function at a time, short code lines,
  low call-graph depth.
- Larger models: Tier 1 plus selected Tier 2 blocks, bounded call graphs, and
  filtered detail JSON when necessary.

The MCP server may provide a `compact` option, but it should remove optional
metadata rather than rename core fields. Stable field names improve tool use
across models.

## Evidence and uncertainty

Responses should distinguish reconstructed behavior from exact raw data. Useful
metadata includes:

```json
{
  "source": "kismet-bytecode",
  "parse_status": "complete",
  "confidence": "inferred-control-flow",
  "warnings": []
}
```

If an expression is unsupported by `SummaryGenerator`, expose the warning and
offer the raw/detail endpoint instead of presenting an incomplete function as
complete.

## What the existing commands contribute

- `gen-cfg` supplies readable pseudo-code and basic-block successors.
- `gen-cfg-tree` supplies the class hierarchy and rendered CFG pages.
- `to-json`/`gen-json-tree` supplies the complete UAssetAPI serialization.
- `to-llm-json` supplies one compact semantic asset response.
- `gen-llm-tree` supplies per-asset semantic responses and `manifest.json`.

The tree exporter does not create documents for assets with no semantic
content. MCP indexing should use the manifest for skipped-asset accounting and
index only documents with an output path. Raw asset inventory, if needed, is a
separate concern from the LLM semantic document set.

An MCP indexer can consume these projections, but a direct integration with the
parsed `UAsset` object is preferable because it can create explicit call,
property, and asset-reference edges without reparsing rendered output.

## Testing

The MCP server has three test layers, all runnable from the workspace root:

```bash
python3 -m unittest discover -s mcp_server -p 'test_*.py'
```

- `mcp_server/test_server.py`: in-process unit tests with a synthesized catalog.
- `mcp_server/test_integration.py`: end-to-end tests that launch the real
  server process, speak JSON-RPC over stdio, and verify the initialize/tools/
  call lifecycle, error responses, and ping.
- `mcp_server/fixtures/tree/`: a static, git-tracked fixture catalog (one
  complete, one partial, one empty asset) so integration tests and new tools
  have deterministic data without requiring the full generated tree.

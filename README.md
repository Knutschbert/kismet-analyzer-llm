# kismet-analyzer LLM workspace

This directory is a working area for presenting cooked Unreal assets to an LLM
and for the MCP server built on that output.

## Contents

- [`docs/`](docs/) - MCP design, semantic graph schema, and LLM context guidance
- [`mcp_server/`](mcp_server/) - read-only MCP server over the generated
  semantic JSON (see [`mcp_server/README.md`](mcp_server/README.md))
- [`bin/`](bin/) - portable release payload used by the batch scripts
- [`output/BaseSpawner/`](output/BaseSpawner/) - example UE4.25 outputs for
  `BaseSpawner.uasset`
- [`scripts/produce-asset.bat`](scripts/produce-asset.bat) - process one asset
- [`scripts/produce-tree.bat`](scripts/produce-tree.bat) - process a complete
  asset directory recursively
- [`scripts/produce-llm.bat`](scripts/produce-llm.bat) - produce semantic LLM
  JSON for one asset or an entire asset directory
- [`scripts/sync-tree-wsl.sh`](scripts/sync-tree-wsl.sh) - copy a generated
  tree to the WSL-native filesystem for fast MCP catalog calls
- [`scripts/run-mcp.bat`](scripts/run-mcp.bat) - start the MCP server against
  the generated tree

## Analyzer binary

The scripts use the self-contained binary in:

```text
bin\kismet-analyzer.exe
```

To use a different build, replace the contents of `bin\` with the contents of
that build's release output directory. No machine-specific path is required.
The current bundled build is approximately 11 MB and includes its required
DLLs and runtime configuration.

You can alternatively set `KISMET_ANALYZER_EXE` to an explicit executable path
for a one-off run.

The scripts default to Unreal Engine **4.25**. Set `KISMET_ANALYZER_EXE` to
override the executable location, or edit `KISMET_UE_VERSION` if processing
assets from another engine version.

## Process one asset

In Windows Explorer, drag **one `.uasset` or `.umap` file** onto
`scripts\produce-asset.bat`. Do not drop a folder onto this script. Or run:

```bat
scripts\produce-asset.bat "P:\path\to\Asset.uasset"
```

By default, output is written beside the asset in:

```text
Asset-outputs\Asset.json
Asset-outputs\Asset.txt
Asset-outputs\Asset.dot
```

An optional second argument selects the output directory:

```bat
scripts\produce-asset.bat "P:\path\to\Asset.uasset" "P:\path\to\output"
```

## Process an asset tree

In Windows Explorer, drag an **unpacked asset folder** onto
`scripts\produce-tree.bat`. The folder should contain `.uasset` files and their
matching `.uexp` files, with any subdirectories you want scanned. The script
recursively scans all `.uasset` and `.umap` files below the dropped folder.

Run:

```bat
scripts\produce-tree.bat "P:\path\to\unpacked-assets" "P:\path\to\analysis-output"
```

The second argument is optional. If omitted, the script creates an
`<input-directory>-kismet-output` directory. The input directory structure is
preserved under the output directory, and each `.uasset`/`.umap` produces JSON,
text CFG, and DOT files.

For example, dropping this folder onto `produce-tree.bat`:

```text
P:\Modding\unchained\wwise\unpacked\TBL
```

will scan all assets under `TBL` and create a matching output tree. The
individual `.uexp` files are not passed to the script; they are loaded
automatically by the analyzer when they are beside their `.uasset` files.

The tree script processes assets one at a time and continues after an asset
fails. A summary of failures is printed at the end.

## Produce semantic LLM JSON

Use `scripts\produce-llm.bat` for the compact model-facing format:

- Drop one `.uasset` or `.umap` file onto it to create `<Asset>.llm.json`.
- Drop an unpacked asset folder onto it to create per-asset `.llm.json` files
  and `manifest.json` recursively.

Examples:

```bat
scripts\produce-llm.bat "P:\path\to\BaseSpawner.uasset"
scripts\produce-llm.bat "P:\path\to\unpacked-assets" "P:\path\to\llm-output"
```

The wrapper uses `bin\kismet-analyzer.exe` and defaults to `VER_UE4_25`.
Without an explicit output argument, generated files are placed under the
git-ignored `output\generated\` directory inside this workspace rather than
beside the extracted assets. Empty semantic assets are omitted; they remain in
`manifest.json` with `parseStatus: "empty"` and no output path. Assets that carry
only CDO default values are omitted too unless `KISMET_LLM_DEFAULTS=1` is set.

For a directory dropped without an explicit output path, the source directory
name is retained under `output\generated\tree\` (for example,
`output\generated\tree\TBL\`) so runs for different games do not collide.

You can override the analyzer and engine version for a session:

```bat
set KISMET_UE_VERSION=VER_UE4_27
set KISMET_ANALYZER_EXE=P:\other\build\kismet-analyzer.exe
```

To also dump each asset's CDO default values (for graph-less, property-only
assets like utility AI behaviors), set `KISMET_LLM_DEFAULTS=1` for the session.
Default values include component and subobject defaults (component-qualified
names like `A_MoveTo_C_0.Acceptance Radius`); Blueprint plumbing, empty
templates, and serialized null/zero values are skipped. Without the flag, default
values are not emitted and output is identical to earlier versions.

```bat
set KISMET_LLM_DEFAULTS=1
```

## MCP guidance

Use the **compact semantic JSON graph** as the canonical model-facing format.
An MCP server should expose it with progressive disclosure:

1. Return a small asset summary first.
2. Retrieve the call graph around a relevant function.
3. Retrieve control-flow blocks only when needed.
4. Fall back to filtered raw JSON or text CFG output for forensic detail.

A working server lives in [`mcp_server/`](mcp_server/) — run it with
`scripts\run-mcp.bat` (Windows) or `KISMET_LLM_DATA=output/generated/tree/TBL
python3 -m mcp_server.server` from this directory. Its tools, defaults, and
verification steps are documented in
[`mcp_server/README.md`](mcp_server/README.md).

The existing formats have different roles:

| Format | Recommended role |
| --- | --- |
| Semantic JSON graph | Primary MCP response and index format |
| `gen-cfg` `.txt` | Human-readable behavior/evidence view |
| UAssetAPI JSON | Exact parser/object dump and round-trip input |
| DOT/SVG/HTML | Human visualization |

See [`docs/schema.md`](docs/schema.md) for the response structure and
[`docs/mcp-design.md`](docs/mcp-design.md) for MCP tools, retrieval levels, and
context-minimization rules. The semantic export represents recovered Kismet
bytecode behavior; it is not guaranteed to reproduce the original Blueprint
editor node graph or layout.

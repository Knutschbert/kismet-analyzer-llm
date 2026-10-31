#!/bin/sh
# Sync the generated semantic tree from the Windows drive to the WSL-native
# filesystem so MCP catalog calls avoid 9p I/O (measured: index rebuild
# ~150 s -> ~7 s, find_assets ~19 s -> ~0.3 s per call).
#
# Usage:
#   scripts/sync-tree-wsl.sh [tree-name]     (default: TBL)
set -e

name="${1:-TBL}"
src="$(cd "$(dirname "$0")/.." && pwd)/output/generated/tree/$name"
dest="$HOME/kismet-llm-data/$name"

if [ ! -f "$src/manifest.json" ]; then
    echo "no generated tree at $src" >&2
    echo "generate one first: scripts\\produce-llm.bat <unpacked-assets-dir>" >&2
    exit 1
fi

mkdir -p "$dest"
rsync -a --delete --exclude '*.sqlite3' "$src/" "$dest/"
echo "synced $src -> $dest"
echo ""
echo "point the MCP server at the copy (index stays ext4 too, inside the data root):"
echo "  KISMET_LLM_DATA=$dest python3 -m mcp_server.server"

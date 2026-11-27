# v3 map-doc pruning — pointer

Make map (`.umap`) docs lean by dropping engine art/geometry/scaffolding exports (~86% of map
objects) while keeping gameplay actors/logic. **Maps only** — no other asset types.

- **Executable spec** (policy, census evidence, generate/validate/release, consumers):
  `/mnt/p/code/chiv/2026/chivalry2-workbench/docs/research/kismet/notes/2026-11-27-v3-map-pruning-plan.md`
- **Tickets:** `kismet-llm-rb6` (epic) → `.1` generator policy, `.2` validation (this tracker).
  Bot-side: `caib-2yg.20`. Builds on the v2 reingestion plan (same workbench notes dir).
- **Where to implement:** the exported-object serialization in the **C# analyzer source** at
  `/mnt/p/code/chiv/2026/assets/kismet-analyzer`; rebuild the exe into this repo's `bin/`, then
  regenerate via `scripts/produce-llm.bat` (with `KISMET_LLM_DEFAULTS=1`) to a **new** staging dir.
- **Key calls:** keep `SplineComponent` (spawners/pushables); drop `SplineMeshComponent`. Keep any
  object with Kismet functions / `*_C` class / the map descriptor; keep a per-map aggregate count of
  pruned classes. Owner-approved regeneration only.

#!/usr/bin/env python3
"""Produce the v3 kismet LLM tree from a v2 tree by pruning map-doc art/geometry exports.

This is the one-time v2->v3 post-process for ticket kismet-llm-rb6. It is EQUIVALENT to
re-running the pruned `gen-llm-tree` generator: the prune is deterministic on each exported
object's `class` (already present in v2 docs), so filtering the v2 JSON yields byte-identical
results to running the C# analyzer with MapPrunePolicy -- without re-parsing cooked assets or
needing the unpacked TBL Content root.

The policy here MUST stay in sync with MapPrunePolicy.cs in the kismet-analyzer source repo
(the canonical generator). See:
  /mnt/p/code/chiv/2026/chivalry2-workbench/docs/research/kismet/notes/2026-11-27-v3-map-pruning-plan.md

Scope: MAPS ONLY (asset endswith .umap). Non-map docs and all other files are copied unchanged.
Dropped objects are replaced by a per-map `prunedExportCounts` aggregate so map density stays
answerable. Run with -I (isolated) since it reads an untrusted data tree.

Usage:
  python3 -I prune_map_tree.py <v2-tree> <v3-out> [--only SUBPATH] [--dry-run] [--limit N]
"""
from __future__ import annotations
import argparse, json, os, shutil, sys
from collections import Counter

# --- Policy: mirror of MapPrunePolicy.cs (kismet-llm-rb6.1). Default is KEEP. ------------------
DROP_EXACT = {
    # meshes / geometry / scene scaffolding
    "StaticMeshComponent", "StaticMeshActor", "InstancedStaticMeshComponent",
    "HierarchicalInstancedStaticMeshComponent", "FoliageInstancedStaticMeshComponent",
    "SkeletalMeshComponent", "SceneComponent",
    # bare collision primitives (TBLBoxCollision is gameplay -> kept)
    "BoxComponent", "CapsuleComponent", "SphereComponent",
    # physics / brush / model
    "Model", "BodySetup", "BrushComponent", "Brush",
    # decals
    "DecalComponent", "DecalActor",
    # dynamic/baked materials + baked textures
    "MaterialInstanceDynamic", "LandscapeMaterialInstanceConstant", "MaterialInstanceConstant",
    "LightMapTexture2D", "ShadowMapTexture2D", "Texture2D", "TextureCube", "Texture2DArray",
    # particles / vfx
    "ParticleSystemComponent", "TBLParticleSystemComponent", "Emitter", "ParticleSystem",
    # audio (Wwise)
    "AkComponent", "AkAmbientSound", "AkReverbVolume", "AkAcousticPortal",
    "AkLateReverbComponent", "AkRoomComponent", "AkSurfaceReflectorSetComponent", "AkGeometryComponent",
    # splines: mesh only (SplineComponent is kept)
    "SplineMeshComponent",
    # generic navigation (TBLNavModifierComponent is gameplay -> kept)
    "NavLinkCustomComponent", "NavLinkProxy", "NavModifierVolume", "NavMeshBoundsVolume",
    "RecastNavMesh", "NavLinkComponent",
    # editor-only gizmos
    "ArrowComponent", "BillboardComponent", "DrawSphereComponent", "DrawFrustumComponent",
    "TextRenderComponent", "BookMark",
    # rendering captures / terrain geometry / lightmass scaffolding
    "SphereReflectionCapture", "SphereReflectionCaptureComponent", "PlanarReflectionComponent",
    "LandscapeComponent", "LandscapeHeightfieldCollisionComponent", "LandscapeGizmoActiveActor",
    "LightmassImportanceVolume", "LightmassPortal", "PrecomputedVisibilityVolume",
    # explicit lights (plus the endswith-Light rule below)
    "PointLightComponent", "PointLight", "SpotLightComponent", "SpotLight",
    "DirectionalLightComponent", "DirectionalLight", "SkyLightComponent", "SkyLight",
    "RectLightComponent", "RectLight", "SkyAtmosphereComponent", "ExponentialHeightFogComponent",
}
DROP_PREFIX = ("MovieScene",)
KEEP_DESCRIPTOR = {"World", "WorldSettings", "TBLWorldSettings", "LevelScriptActor"}


def should_drop(cls: str | None) -> bool:
    if not cls:
        return False
    if cls.endswith("_C"):                       # Blueprint instance: keep
        return False
    if cls in KEEP_DESCRIPTOR:                    # map descriptor: keep
        return False
    if cls in DROP_EXACT:
        return True
    if cls.startswith(DROP_PREFIX):
        return True
    return cls.endswith("Light") or cls.endswith("LightComponent")


def is_map(doc: dict) -> bool:
    return str(doc.get("asset", "")).endswith(".umap")


def prune_doc(doc: dict) -> tuple[dict, int, int]:
    """Return (possibly-rewritten doc, kept_count, dropped_count). No-op for non-maps."""
    objs = doc.get("exportedObjects")
    if not is_map(doc) or not objs:
        return doc, (len(objs) if objs else 0), 0
    kept, pruned = [], Counter()
    for o in objs:
        if should_drop(o.get("class")):
            pruned[o.get("class") or "<unknown>"] += 1
        else:
            kept.append(o)
    if not pruned:
        return doc, len(kept), 0
    # rebuild preserving key order, inserting prunedExportCounts right after exportedObjects
    out = {}
    for k, v in doc.items():
        if k == "exportedObjects":
            out[k] = kept
            out["prunedExportCounts"] = dict(sorted(pruned.items(), key=lambda kv: (-kv[1], kv[0])))
        else:
            out[k] = v
    return out, len(kept), sum(pruned.values())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src"); ap.add_argument("dst")
    ap.add_argument("--only", default="", help="restrict to this subpath under src")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="stop after N map docs (dry-run sizing)")
    a = ap.parse_args()

    src, dst = os.path.abspath(a.src), os.path.abspath(a.dst)
    scan_root = os.path.join(src, a.only) if a.only else src
    files = docs = maps_pruned = 0
    kept_objs = dropped_objs = 0
    src_map_bytes = dst_map_bytes = 0

    for dp, _, fns in os.walk(scan_root):
        for fn in sorted(fns):
            sp = os.path.join(dp, fn)
            rel = os.path.relpath(sp, src)
            op = os.path.join(dst, rel)
            files += 1
            if not fn.endswith(".llm.json"):
                if not a.dry_run:
                    os.makedirs(os.path.dirname(op), exist_ok=True)
                    shutil.copy2(sp, op)
                continue
            with open(sp, encoding="utf-8") as f:
                doc = json.load(f)
            docs += 1
            mapped = is_map(doc) and doc.get("exportedObjects")
            new_doc, kc, dc = prune_doc(doc)
            if mapped:
                src_map_bytes += os.path.getsize(sp)
                kept_objs += kc; dropped_objs += dc
                if dc:
                    maps_pruned += 1
            text = json.dumps(new_doc, indent=2, ensure_ascii=False)
            if mapped:
                dst_map_bytes += len(text.encode("utf-8"))
            if not a.dry_run:
                os.makedirs(os.path.dirname(op), exist_ok=True)
                with open(op, "w", encoding="utf-8") as f:
                    f.write(text)
            if a.limit and maps_pruned >= a.limit:
                break
        if a.limit and maps_pruned >= a.limit:
            break

    tot = kept_objs + dropped_objs
    print(f"{'DRY-RUN ' if a.dry_run else ''}files={files} docs={docs} maps_pruned={maps_pruned}")
    if tot:
        print(f"map exported-objects: kept={kept_objs} dropped={dropped_objs} "
              f"({dropped_objs/tot*100:.1f}% dropped)")
    if src_map_bytes:
        print(f"map doc bytes: {src_map_bytes/1e6:.0f}MB -> {dst_map_bytes/1e6:.0f}MB "
              f"({dst_map_bytes/src_map_bytes*100:.1f}% of original, "
              f"{src_map_bytes/max(dst_map_bytes,1):.1f}x smaller)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

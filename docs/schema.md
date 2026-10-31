# Semantic asset graph schema

The schema is intentionally small and model-oriented. It should contain names
and relationships that help answer questions, not every field needed to write a
`.uasset` back to disk.

The exporter currently emits these top-level fields:

```text
schemaVersion, source, parseStatus, asset, class, parent, engineVersion,
properties, defaultValues, functions, callEdges, diagnostics
```

`defaultValues` is optional. It is present only when the export is generated
with `--defaults`, in which case it carries the asset's CDO (Class Default
Object) property values — the serialized defaults an actor or behavior initializes
itself to. This lets graph-less, property-only assets (for example utility AI
behavior blueprints) still surface their configuration.

Component and subobject defaults are included too: serialized component template
exports (capsules, SCS component templates, construction-script components) and
instanced subobjects referenced by the CDO (for example an AI action or target
generator instance) are emitted with component-qualified names:

```json
{"name": "A_MoveTo_C_0.Acceptance Radius", "type": "Float", "value": "150"}
```

Blueprint plumbing (SimpleConstructionScript, SCS_Node, the Blueprint marker
itself), empty templates, and serialized null/zero values are skipped.

## Asset summary

```json
{
  "schemaVersion": 1,
  "source": "kismet-bytecode",
  "parseStatus": "complete",
  "asset": "/Game/Characters/BP_Player",
  "class": "BP_Player_C",
  "parent": "Character",
  "properties": [
    {"name": "Health", "type": "Float"},
    {"name": "bDead", "type": "Bool"}
  ],
  "functions": [
    {
      "name": "TakeDamage",
      "calls": ["Die"],
      "reads": ["Health"],
      "writes": ["Health", "bDead"],
      "branches": 1
    }
  ],
  "callEdges": [
    {"from": "TakeDamage", "to": "Die"}
  ]
}
```

## Function control flow

Return this only for a selected function or selected block range:

```json
{
  "schemaVersion": 1,
  "source": "kismet-bytecode",
  "parseStatus": "complete",
  "asset": "/Game/Characters/BP_Player",
  "function": "TakeDamage",
  "blocks": [
    {
      "id": 0,
      "code": ["Health = Health - Damage"],
      "successors": [
        {"to": 1, "when": "Health > 0"},
        {"to": 2, "when": "Health <= 0"}
      ]
    },
    {
      "id": 2,
      "code": ["call Die()"],
      "successors": []
    }
  ]
}
```

## Default values (CDO)

Present only when generated with `--defaults`. Each entry is `name`, `type`
(the UAssetAPI property type), and a compact `value` string. Containers are
flattened recursively and object references are resolved to readable names:

```json
{
  "defaultValues": [
    {"name": "Weight", "type": "Int", "value": "5"},
    {
      "name": "Considerations",
      "type": "Array",
      "value": "[UtilityAI_Consideration(Name=Prefer further if close, Input=I_Distance_0, Curve=UtilityAI_Curve(m=0.4, k=1, c=0, b=0.6, Type=EUtilityAI_Curve::Linear)), ...]"
    },
    {"name": "Action", "type": "Object", "value": "A_MoveTo_C_0"},
    {"name": "TargetGenerator", "type": "Object", "value": "TG_PointsAroundActorsWithAITag_0"},
    {"name": "A_MoveTo_C_0.Acceptance Radius", "type": "Float", "value": "150"},
    {"name": "TG_PointsAroundActorsWithAITag_0.SpaceBetween", "type": "Float", "value": "500"}
  ]
}
```

Floats use invariant decimal points (`.`), never locale-specific commas, so the
payload stays portable across machines. Values that could not be rendered fall
back to the property's readable string rather than a raw index.

## Representation rules

- Resolve imports and exports to readable names whenever possible.
- Keep bytecode addresses as local evidence, not as the primary identity.
- Use explicit fields such as `calls`, `reads`, `writes`, `callEdges`, and
  `successors`.
- Omit empty optional fields from summary responses.
- Keep raw package indices, serialization offsets, and parser-specific fields in
  a separate detail response.
- Represent unknown or failed parsing explicitly, for example with
  `"parse_status": "raw"`, rather than silently dropping data.
- Treat `parseStatus: "partial"` and the `diagnostics` array as authoritative
  indications that some behavior could not be fully reconstructed.
- Preserve the original asset path and function names for citations and lookup.

## Response tiers

| Tier | Contents | Typical use |
| --- | --- | --- |
| 0 | Asset identity, properties, function names, direct call edges | Orientation |
| 1 | One function summary and nearby callers/callees | Behavioral questions |
| 2 | Selected CFG blocks and pseudo-code | Detailed reasoning |
| 3 | Filtered UAssetAPI JSON or `.txt` CFG | Verification and forensics |
| 4 | Raw/unparsed data and full asset JSON | Parser/debugging workflows |

Tree exports omit assets whose semantic result has no properties, functions,
call edges, or diagnostics, unless those assets carry `defaultValues` from a
`--defaults` run. Without `--defaults`, a graph-less property-only asset is
recorded in `manifest.json` with `parseStatus: "empty"`, `reason:
"no_semantic_content"`, and `output: null`. With `--defaults`, the same asset
gains a `defaultValues` list, becomes semantically non-empty, and produces a
document. An explicitly requested `to-llm-json` call still returns the empty
document when `--defaults` is not passed.

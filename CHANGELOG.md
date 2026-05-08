# Changelog

All notable changes to RF Static Mesh Tools.

---

## v1.4.5 — 2026-05-08

### Fixed
- `CHUNK_RFLAGS` corrected to `0x00518C41` to match Redux's V3M exporter
  (was `0x00400C41`). Per-chunk descriptors in exported files now match
  Redux output exactly.
- Per-tri edge flag in triangle data now writes `0x02` per Redux convention
  (was `0`). Brings exported triangles in line with stock RF files.
- Texture name resolution now checks the image node connected to
  Principled BSDF Base Color before falling back to material name.
  Models imported from outside this tool with proper image-textured
  materials will now export with the actual texture filename instead
  of getting Blender's `.001`-suffixed material name.

### Changed
- `ImportHelper` and `ExportHelper` operators now declare `filepath`
  explicitly for Blender 5.0+ compatibility.
- `register()` and `unregister()` wrap each class in try/except so
  partial registration failures log to console instead of leaving
  Blender in a half-broken state.

---

## v1.4.4 — 2026-05-07

### Fixed
- V3M import parser rewritten to mirror Redux's `UnpackLodDataBlock`
  exactly. Two bugs fixed:
  - Missing 16-byte alignment between sections (positions / normals /
    UVs / triangles). Without alignment, the first vertex was misread
    by 4 bytes and every subsequent read landed in pad bytes — produced
    junk geometry on multi-section meshes like Cauldron.
  - Missing TrianglePlanes block handling (LOD flag 0x20). When set,
    `cf × 16` bytes of plane data sit between triangle indices and
    the same-position offsets block; previously skipped this and read
    garbage afterwards.
- Triangle indices now read as `<4H>` (3 indices + edge flags), matching
  the on-disk layout.

### Verified
- Cauldron: 517 verts, 360 triangles, 0 degenerate, 0 oversaturated edges.

---

## v1.4.3 — 2026-05-02

### Added
- Material auto-rename from texture name. Set Material Flags operator
  now has a `tex_name` field; editing it stores the full filename
  (e.g. `wood01.tga`) as a custom property `rf_mat_N_name` for export,
  and renames the material slot to the stem (`wood01`) since Blender's
  `.001` suffix system conflicts with dots in material names.
- Set Material Flags panel now shows the export texture name on each row.

---

## v1.4.2 — 2026-05-01

### Fixed
- Vertex deduplication on both V3M and RFG export was keyed by
  `(vertex_index, loop_index)`, which made every face isolated and
  produced 3-4× the expected vertex count.
  - **V3M dedup** now keyed by `(vertex_index, uv_tuple)` matching
    Redux's `AddVertex` pattern. Faces sharing position and UV merge
    correctly; UV seams properly split.
  - **RFG dedup** now keyed by `vertex_index` alone, since RFG stores
    UVs per face corner separately.

---

## v1.4.0 → v1.4.1

Initial public releases. V3M import / export, RFG export, prop point
and collision sphere helpers, material flag editor.

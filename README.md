# RF Static Mesh Tools

A Blender add-on for importing and exporting Red Faction `.v3m` static meshes and `.rfg` groups — without requiring 3ds Max or any Volition tools.


![Version](https://img.shields.io/badge/version-1.4.5-blue) ![Blender](https://img.shields.io/badge/Blender-4.0--5.0-orange) ![License](https://img.shields.io/badge/license-MIT-green)

---

## What is this?

Red Faction's `.v3m` format stores static meshes (props, world geometry, vehicles). The `.rfg` format stores brush groups for the RED level editor. Previously the only way to author these was via Volition's 3ds Max plugin, which is decades out of date and not freely available.

This add-on lets you do both directly in Blender:

- **Import V3M** files: read all submeshes, LODs, materials, prop points, and collision spheres
- **Export V3M** files: convert your Blender meshes to RF-ready binary, with auto-splitting for large meshes
- **Export RFG** groups: ship brushes straight into the RED editor
- **Material flag editor**: set base / alpha / fullbright per slot from a sidebar panel
- **Prop point and collision sphere helpers**: add the `PP_` and `CS_` empties RF expects, with the right naming convention

## Features

### V3M import
- Reads multi-submesh, multi-LOD V3M files
- Imports all materials with proper texture name preservation (no `.001` Blender suffixes)
- Optional import of prop points and collision spheres
- Selects high / medium / low LOD on import
- Tested against stock RF assets (Cauldron, Jeep, etc.)

### V3M export
- Auto-split for meshes exceeding RF's per-chunk limits
- Per-material flag preservation (base / alpha / fullbright)
- Texture name resolution from custom property → image node filename → material name
- Correct triangle winding flip and 16-byte data block alignment
- Uses Redux's per-chunk render flags for stock-file compatibility

### RFG export
- Convert Blender meshes to RF brush groups for the RED editor
- Per-face UV preservation
- Handles winding correctly for the RF coordinate system

### UI helpers
- Sidebar panel: `View3D > Sidebar > RF Static`
- Set Material Flags operator with texture name editing
- Add Prop Point / Add Collision Sphere operators with auto-naming

## Installing

1. Download `rf_static_mesh_tools_v1_4_5.zip` from the [Releases](../../releases) page.
2. Blender → Edit → Preferences → Add-ons → Install from Disk
3. Pick the zip, enable "RF Static Mesh Tools" in the list.
4. The toolbar lives in `View3D > Sidebar > RF Static`.

## Quick start

**Importing a V3M:**
1. File → Import → RF V3M (.v3m)
2. Pick the file, choose LOD level, hit Import

**Exporting your mesh as V3M:**
1. Select the mesh objects you want to export
2. Open the RF Static panel in the sidebar (press N)
3. Click "Export V3M" or use File → Export → RF V3M (.v3m)

**Setting up materials for RF:**
1. Add an Image Texture node connected to the Principled BSDF Base Color
2. Point it at your `.tga` / `.dds` / `.bmp` / `.pcx` file
3. The exporter will read the filename automatically — no need to rename your material to match

## Coordinate system

RF uses a left-handed Y-up coordinate system; Blender uses right-handed Z-up. This add-on handles the conversion automatically — your meshes will appear upright and properly oriented in Blender after import, and re-export round-trips cleanly.

For technical details on the RF / Blender / glTF coordinate pipeline, see the [RF Coordinate Bible](https://github.com/RomekRF/RF-Character-Tools) (eventually to be merged into a unified RF tools doc).

## Compatibility

- Blender 4.0 — 5.0+
- Tested on Blender 5.0.1
- Round-trip integrity tested on stock RF V3M files

## Credits

- Format reference: Redux (Goober), rafalh's vmesh / v3d-tool
- Built for the [Alpine Faction](https://github.com/GooberRF/alpinefaction) community
- Companion to [RF Character Tools](https://github.com/RomekRF/RF-Character-Tools) and [RF VFX Tools](https://github.com/RomekRF/VFX_Exporter)

## License

MIT — see [LICENSE](LICENSE).

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for version history.

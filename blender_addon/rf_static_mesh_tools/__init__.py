bl_info = {
    "name": "RF Static Mesh Tools",
    "author": "Romek",
    "version": (1, 4, 6),
    "blender": (4, 0, 0),
    "location": "File > Import/Export | Sidebar > RF Static",
    "description": "Import and export Red Faction V3M static meshes and RFG groups",
    "category": "Import-Export",
}

import bpy
import bmesh
import struct
import os
import math
from bpy.props import (StringProperty, BoolProperty, EnumProperty,
                       IntProperty, FloatProperty)
from bpy_extras.io_utils import ImportHelper, ExportHelper
from mathutils import Vector

# ─────────────────────────────────────────────────────────────────────────────
#  Constants
# ─────────────────────────────────────────────────────────────────────────────

V3M_SIG_BYTES  = b'D3FR'           # "RF3D" reversed on disk
V3M_VER_BYTES  = b'\x00\x00\x04\x00'
SEC_SUBM       = b'MBUS'           # "SUBM" reversed
SEC_CSPH       = b'HPSC'           # "CSPH" reversed
SEC_SIZE_ZERO  = b'\x00\x00\x00\x00'
CHUNK_RFLAGS   = 0x00518C41   # matches Redux V3MExporter (decimal 5344321)
MAT_BASE       = 0x01
MAT_ALPHA      = 0x08
MAT_FULLBRIGHT = 0x10
# Split limits — constrained by uint16 byte-size fields in outer descriptors:
#   cvecs   = cv*12 must fit uint16 → cv <= 5461  (5461*12=65532 ✓)
#   cfalloc = cf*8  must fit uint16 → cf <= 8191  (8191*8=65528 ✓)
CHUNK_VERT_LIMIT = 5461
CHUNK_TRI_LIMIT  = 8191


def rf_to_bl(x, y, z):
    return Vector((-x, -z, y))

def bl_to_rf(v):
    return (-v.x, v.z, -v.y)

def pad16(n):
    return (n + 15) & ~15


# ─────────────────────────────────────────────────────────────────────────────
#  V3M Parser
# ─────────────────────────────────────────────────────────────────────────────

def _parse_v3m(filepath):
    with open(filepath, 'rb') as f:
        data = f.read()

    if data[0:4] != V3M_SIG_BYTES:
        raise ValueError(f"Not a V3M file (sig={data[0:4]})")

    off = 8
    nsub, nverts, ntris, unk0, nmats_hdr, unk1, unk2, ncsph = \
        struct.unpack_from("<7Ii", data, off); off += 32

    result = {'submeshes': [], 'cspheres': []}

    while off < len(data) - 4:
        marker = data[off:off+4]
        if marker == b'\x00\x00\x00\x00':
            break
        sec_size_field = struct.unpack_from("<I", data, off+4)[0]
        sec_start = off + 8
        off += 8

        if marker == SEC_SUBM:
            sm, consumed = _parse_subm(data, sec_start)
            result['submeshes'].append(sm)
            off = sec_start + consumed
        elif marker == SEC_CSPH:
            cs = _parse_csph(data, sec_start)
            result['cspheres'].append(cs)
            off = sec_start + 44
        else:
            if sec_size_field > 0:
                off = sec_start + sec_size_field
            else:
                break

    return result


def _parse_subm(data, off):
    start = off
    name   = data[off:off+24].split(b'\x00')[0].decode('latin-1'); off += 24
    parent = data[off:off+24].split(b'\x00')[0].decode('latin-1'); off += 24
    sub_ver, num_lods = struct.unpack_from("<ii", data, off); off += 8
    lod_dists = [struct.unpack_from("<f", data, off+i*4)[0] for i in range(num_lods)]
    off += num_lods * 4
    bbox = struct.unpack_from("<10f", data, off); off += 40

    lods = []
    for li in range(num_lods):
        lod, off = _parse_lod(data, off)
        lod['dist'] = lod_dists[li]
        lods.append(lod)

    off += 4  # 4 zero bytes

    # Texture list
    ntex = struct.unpack_from("<I", data, off)[0]; off += 4
    tex_list = []
    for _ in range(ntex):
        chunk_id = data[off]; off += 1
        end = data.index(b'\x00', off)
        tex_list.append((chunk_id, data[off:end].decode('latin-1')))
        off = end + 1

    # Material detail: name(64) + emissive(16) + flags(4) = 84 bytes
    nmat = struct.unpack_from("<I", data, off)[0]; off += 4
    materials = []
    for _ in range(nmat):
        mat_name  = data[off:off+64].split(b'\x00')[0].decode('latin-1')
        emissive  = struct.unpack_from("<4f", data, off+64)
        flags     = struct.unpack_from("<I",  data, off+80)[0]
        materials.append({'name': mat_name, 'emissive': emissive, 'flags': flags})
        off += 84

    # Prop points: name(28) only
    npp = struct.unpack_from("<I", data, off)[0]; off += 4
    prop_points = []
    for _ in range(npp):
        pp_name = data[off:off+28].split(b'\x00')[0].decode('latin-1')
        prop_points.append(pp_name)
        off += 28

    sm = {
        'name': name, 'parent': parent, 'bbox': bbox,
        'lods': lods, 'tex_list': tex_list,
        'materials': materials, 'prop_points': prop_points,
    }
    return sm, off - start


def _parse_lod(data, off):
    """Parse a V3M LOD descriptor.

    Mirrors Redux's V3mParser.UnpackLodDataBlock: every internal section
    (positions, normals, UVs, tris, planes, same_pos, bone_links, orig_map)
    is followed by padding to a 16-byte boundary *within* the data block.
    Failing to honor that padding makes every read after positions land
    inside pad bytes, producing junk geometry.
    """
    LOD_FLAG_ORIG_MAP   = 0x01
    LOD_FLAG_TRI_PLANES = 0x20

    flags   = struct.unpack_from("<I", data, off)[0]; off += 4
    nv      = struct.unpack_from("<i", data, off)[0]; off += 4
    nc      = struct.unpack_from("<H", data, off)[0]; off += 2
    db_size = struct.unpack_from("<i", data, off)[0]; off += 4

    db_start = off
    off += db_size

    unk1 = struct.unpack_from("<i", data, off)[0]; off += 4

    # Outer chunk descriptors (18 bytes each)
    outer_chunks = []
    for _ in range(nc):
        cv, cf, cvecs, cfalloc, csame, cwi, cuvs, crf = \
            struct.unpack_from("<6HHI", data, off)
        outer_chunks.append({
            'nv': cv, 'nf': cf,
            'cvecs': cvecs, 'cfalloc': cfalloc,
            'csame': csame, 'cwi': cwi, 'cuvs': cuvs,
        })
        off += 18

    # ── Iterate the data block.
    # Cursor `dp` is the absolute file offset; alignment is computed against
    # the data-block-relative position (dp - db_start), matching Redux which
    # uses MemoryStream over the data block bytes.
    def align16(p):
        rel = (p - db_start) & 0xF
        return p + ((16 - rel) & 0xF)

    dp = db_start

    # Per-chunk headers: 56 bytes each (we don't use the contents here, but
    # we need to skip past them and the trailing 16-byte alignment pad).
    dp += nc * 56
    dp = align16(dp)

    verts, uvs, norms, tris = [], [], [], []

    for ch in outer_chunks:
        cv      = ch['nv']
        cf      = ch['nf']
        cvecs   = ch['cvecs']
        cuvs    = ch['cuvs']
        cfalloc = ch['cfalloc']
        csame   = ch['csame']
        cwi     = ch['cwi']

        # Positions (cv vec3f), pad to 16
        ch_verts = [struct.unpack_from("<3f", data, dp + i*12) for i in range(cv)]
        dp += cvecs
        dp = align16(dp)

        # Normals (cv vec3f), pad to 16
        ch_norms = [struct.unpack_from("<3f", data, dp + i*12) for i in range(cv)]
        dp += cvecs
        dp = align16(dp)

        # UVs (cv vec2f), pad to 16
        ch_uvs = [struct.unpack_from("<2f", data, dp + i*8) for i in range(cv)]
        dp += cuvs
        dp = align16(dp)

        # Triangles: 4 ushorts each (i0, i1, i2, edge_flags), pad to 16
        vb = len(verts)
        for ti in range(cf):
            a, b, c, _flg = struct.unpack_from("<4H", data, dp + ti*8)
            tris.append((vb + a, vb + b, vb + c))
        dp += cfalloc
        dp = align16(dp)

        # Optional triangle planes: cf * (vec3f normal + f dist) = 16 bytes per tri
        if flags & LOD_FLAG_TRI_PLANES:
            dp += cf * 16
            dp = align16(dp)

        # same_pos_vertex_offsets, pad to 16
        dp += csame
        dp = align16(dp)

        # Optional bone_links (only when WiAlloc > 0), pad to 16
        if cwi > 0:
            dp += cwi
            dp = align16(dp)

        # Optional orig_map (per-LOD vertex count, 2 bytes each), pad to 16
        if flags & LOD_FLAG_ORIG_MAP:
            dp += nv * 2
            dp = align16(dp)

        verts.extend(ch_verts)
        uvs.extend(ch_uvs)
        norms.extend(ch_norms)

    return {
        'flags': flags, 'nv': nv,
        'verts': verts, 'uvs': uvs, 'norms': norms, 'tris': tris,
        'chunks': outer_chunks,
    }, off


def _parse_csph(data, off):
    cs = {}
    cs['name']   = data[off:off+24].split(b'\x00')[0].decode('latin-1'); off += 24
    cs['parent'] = struct.unpack_from("<i",  data, off)[0]; off += 4
    cs['pos']    = struct.unpack_from("<3f", data, off); off += 12
    cs['radius'] = struct.unpack_from("<f",  data, off)[0]
    return cs


# ─────────────────────────────────────────────────────────────────────────────
#  Build Blender scene
# ─────────────────────────────────────────────────────────────────────────────

def _build_mesh(v3m, lod_idx=0, sm_idx=0):
    sm  = v3m['submeshes'][sm_idx]
    li  = min(lod_idx, len(sm['lods']) - 1)
    lod = sm['lods'][li]

    name = sm['name'] or "RFMesh"
    bm   = bmesh.new()
    uv_layer = bm.loops.layers.uv.new("UVMap")

    bverts = [bm.verts.new(rf_to_bl(*v)) for v in lod['verts']]
    bm.verts.ensure_lookup_table()

    for tri in lod['tris']:
        a, b, c = tri
        # Skip degenerate tris (V3M can include sentinel/edge-only entries
        # with repeated indices, e.g. (0,0,1)) — bmesh.faces.new would raise.
        if a == b or b == c or a == c:
            continue
        try:
            face = bm.faces.new([bverts[a], bverts[b], bverts[c]])
        except ValueError:
            # Duplicate face: BMesh refuses two faces with the same vert set.
            continue
        for loop, vi in zip(face.loops, [a, b, c]):
            u, v = lod['uvs'][vi]
            loop[uv_layer].uv = (u, 1.0 - v)

    mesh = bpy.data.meshes.new(name)
    bm.to_mesh(mesh)
    bm.free()

    obj = bpy.data.objects.new(name, mesh)

    for mi, mat in enumerate(sm['materials']):
        bmat = bpy.data.materials.get(mat['name'])
        if not bmat:
            bmat = bpy.data.materials.new(mat['name'])
            bmat.use_nodes = True
            if mat['flags'] & MAT_ALPHA:
                bmat.blend_method = 'BLEND'
                bmat.shadow_method = 'NONE'
        obj.data.materials.append(bmat)
        obj[f'rf_mat_{mi}_flags'] = mat['flags']
        obj[f'rf_mat_{mi}_name']  = mat['name']

    obj['rf_submesh_name'] = sm['name']
    obj['rf_lod_imported'] = li
    return obj


def _spawn_empties(v3m, parent_obj, context, sm_idx=0):
    sm = v3m['submeshes'][sm_idx]
    for pp_name in sm['prop_points']:
        e = bpy.data.objects.new("PP_" + pp_name, None)
        e.empty_display_type = 'ARROWS'
        e.empty_display_size = 0.05
        e.parent = parent_obj
        context.scene.collection.objects.link(e)
    for cs in v3m['cspheres']:
        e = bpy.data.objects.new("CS_" + cs['name'], None)
        e.empty_display_type = 'SPHERE'
        e.empty_display_size = cs['radius']
        e.location = rf_to_bl(*cs['pos'])
        e.parent = parent_obj
        context.scene.collection.objects.link(e)


# ─────────────────────────────────────────────────────────────────────────────
#  Mesh splitting
# ─────────────────────────────────────────────────────────────────────────────

def _split_into_chunks(obj):
    mesh = obj.data
    mesh.calc_loop_triangles()
    uv_data = mesh.uv_layers.active.data if mesh.uv_layers.active else None

    mat_tris = {}
    for lt in mesh.loop_triangles:
        mi = lt.material_index
        mat_tris.setdefault(mi, []).append((lt.vertices, lt.loops))

    chunks = []
    for mi, tri_list in mat_tris.items():
        pending = tri_list
        while pending:
            local_verts = {}
            local_pos   = []
            local_uvs   = []
            local_tris  = []
            remaining   = []

            # Vertex sharing: V3M's data block stores one UV per vertex, so we
            # dedup by (blender_vertex_index, uv) — matching Redux's approach.
            # Vertices that share both position AND UV are merged; UV seams
            # produce splits (which they must, since UVs are per-vertex in V3M).
            # Two faces sharing an edge will now reference the same underlying
            # vertices instead of each face becoming an isolated island.
            for verts_gl, loops_gl in pending:
                # Build the keys this triangle would add
                tri_keys = []
                for v, l in zip(verts_gl, loops_gl):
                    if uv_data:
                        uv = uv_data[l].uv
                        uv_key = (round(uv.x, 6), round(1.0 - uv.y, 6))
                    else:
                        uv_key = (0.0, 0.0)
                    tri_keys.append((v, uv_key))

                new_keys = [k for k in tri_keys if k not in local_verts]
                if (len(local_pos) + len(new_keys) > CHUNK_VERT_LIMIT or
                        len(local_tris) >= CHUNK_TRI_LIMIT):
                    remaining.append((verts_gl, loops_gl))
                    continue

                tri_local = []
                for (v, uv_key) in tri_keys:
                    key = (v, uv_key)
                    if key not in local_verts:
                        local_verts[key] = len(local_pos)
                        co = mesh.vertices[v].co
                        local_pos.append(bl_to_rf(co))
                        local_uvs.append(uv_key)
                    tri_local.append(local_verts[key])
                local_tris.append(tuple(tri_local))

            if local_tris:
                chunks.append({
                    'verts': local_pos,
                    'uvs':   local_uvs,
                    'tris':  local_tris,
                    'mat':   mi,
                })
            pending = remaining

    return chunks


# ─────────────────────────────────────────────────────────────────────────────
#  V3M Writer
# ─────────────────────────────────────────────────────────────────────────────

def _write_v3m(filepath, objects):
    all_subm_bytes = []
    for obj in objects:
        chunks = _split_into_chunks(obj)
        if not chunks:
            continue

        base_name = obj.get('rf_submesh_name', obj.name)[:23]

        # Group chunks into submeshes
        subm_groups = []
        current = []
        current_nv = 0
        for ch in chunks:
            if current_nv + len(ch['verts']) > CHUNK_VERT_LIMIT and current:
                subm_groups.append(current)
                current = []
                current_nv = 0
            current.append(ch)
            current_nv += len(ch['verts'])
        if current:
            subm_groups.append(current)

        for si, group in enumerate(subm_groups):
            sm_name = base_name if si == 0 else f"{base_name}_{si}"
            all_subm_bytes.append(_encode_subm(sm_name, obj, group))

    csph_bytes = []
    if objects:
        for child in objects[0].children:
            if child.name.startswith("CS_"):
                csph_bytes.append(_encode_csph(child))

    nsub  = len(all_subm_bytes)
    ncsph = len(csph_bytes)
    total_mats = nsub  # one material per submesh (each submesh = one material chunk)

    buf = bytearray()
    buf += V3M_SIG_BYTES + V3M_VER_BYTES
    buf += struct.pack("<7Ii", nsub, 0, 0, 0, total_mats, 0, 0, ncsph)

    for sb in all_subm_bytes:
        buf += SEC_SUBM + SEC_SIZE_ZERO + sb

    for cb in csph_bytes:
        buf += SEC_CSPH + struct.pack("<I", 44) + cb

    buf += b'\x00\x00\x00\x00\x00\x00\x00\x00'  # END section (type=0 + size=0)

    with open(filepath, 'wb') as f:
        f.write(buf)

    return nsub


def _v3m_tex_name(obj, mat_index):
    """Get texture name for a material slot, ensuring it has a file extension.

    Resolution order (per RF Coordinate Bible §8 'Texture name source'):
      1. Custom property `rf_mat_N_name` (set on import or by Set Mat Flags op)
      2. Image node filename connected to Base Color / Principled BSDF
      3. Any image node filename in the material
      4. Material slot name (mangled by Blender's .001 suffix system — last resort)
    """
    # 1. Custom property — most authoritative (was set on import)
    name = obj.get(f'rf_mat_{mat_index}_name', '')
    if name:
        return _ensure_tex_ext(name)

    # 2/3. Image node lookup
    if mat_index < len(obj.data.materials) and obj.data.materials[mat_index]:
        mat = obj.data.materials[mat_index]
        node_name = _find_material_image_filename(mat)
        if node_name:
            return _ensure_tex_ext(node_name)
        # 4. Material slot name fallback
        if mat.name:
            return _ensure_tex_ext(mat.name)

    return "default.tga"


def _ensure_tex_ext(name):
    """Append .tga if name lacks a recognized RF texture extension."""
    if not name.lower().endswith(('.tga', '.dds', '.bmp', '.pcx')):
        name += '.tga'
    return name


def _find_material_image_filename(mat):
    """Walk material node tree to find an image filename. Prefer the image
    connected to Principled BSDF Base Color; otherwise return any image found.
    Returns just the filename (basename), no path."""
    if not mat or not mat.use_nodes or not mat.node_tree:
        return ''

    nodes = mat.node_tree.nodes
    links = mat.node_tree.links

    # First pass: find Image Texture node connected (directly or via 1 hop)
    # to Base Color of a Principled BSDF
    for node in nodes:
        if node.type == 'BSDF_PRINCIPLED':
            base_color = node.inputs.get('Base Color')
            if not base_color or not base_color.is_linked:
                continue
            src = base_color.links[0].from_node
            if src.type == 'TEX_IMAGE' and src.image:
                return _image_basename(src.image)
            # One-hop tolerance: e.g. ColorRamp / RGB Curves between texture and BSDF
            for inp in src.inputs:
                if inp.is_linked:
                    inner = inp.links[0].from_node
                    if inner.type == 'TEX_IMAGE' and inner.image:
                        return _image_basename(inner.image)

    # Second pass: any image texture node in the tree
    for node in nodes:
        if node.type == 'TEX_IMAGE' and node.image:
            return _image_basename(node.image)

    return ''


def _image_basename(image):
    """Return the filename component of an image's filepath, or its name."""
    if image.filepath:
        # Strip path, keep just the filename
        return os.path.basename(image.filepath.replace('\\', '/'))
    return image.name


def _encode_subm(name, obj, chunks):
    def padname(s, n):
        b = s.encode('latin-1')[:n-1]
        return b + b'\x00' * (n - len(b))

    buf = bytearray()
    buf += padname(name, 24)
    buf += padname('', 24)
    buf += struct.pack("<ii", 7, 1)   # sub_ver=7, num_lods=1
    buf += struct.pack("<f", 0.0)     # lod_dist

    # Bounding box: offset(3f) + radius(f) + aabbMin(3f) + aabbMax(3f) = 10 floats
    all_verts = [v for ch in chunks for v in ch['verts']]
    if all_verts:
        xs = [v[0] for v in all_verts]
        ys = [v[1] for v in all_verts]
        zs = [v[2] for v in all_verts]
        radius = max((v[0]**2+v[1]**2+v[2]**2)**0.5 for v in all_verts)
        buf += struct.pack("<10f",
            0.0, 0.0, 0.0, radius,
            min(xs), min(ys), min(zs),
            max(xs), max(ys), max(zs))
    else:
        buf += b'\x00' * 40

    # Build chunk field info
    nc  = len(chunks)
    nv  = sum(len(ch['verts']) for ch in chunks)
    chunk_info = []
    for ch in chunks:
        cv      = len(ch['verts'])
        cf      = len(ch['tris'])
        # Outer descriptors store RAW byte counts (matching stock V3M files).
        # The data block pads each section to 16 internally.
        cvecs   = cv * 12              # positions/normals byte count
        cuvs    = cv * 8               # UVs byte count
        cfalloc = cf * 8               # tris byte count
        csame   = cv * 2               # same_pos_vertex_offsets
        cwi     = cv * 8               # weight data (written even for static V3M)
        chunk_info.append({
            'cv': cv, 'cf': cf, 'cvecs': cvecs, 'cuvs': cuvs,
            'cfalloc': cfalloc, 'csame': csame, 'cwi': cwi,
        })

    db = _build_data_block(chunks, chunk_info)

    # LOD header + data block
    buf += struct.pack("<IiHi", 0x00000020, nv, nc, len(db))
    buf += db
    buf += struct.pack("<i", -1)  # unk1

    # Outer chunk descriptors
    for ch, info in zip(chunks, chunk_info):
        buf += struct.pack("<6HHI",
                           info['cv'], info['cf'],
                           info['cvecs'], info['cfalloc'],
                           info['csame'], info['cwi'],
                           info['cuvs'], CHUNK_RFLAGS)

    buf += b'\x00\x00\x00\x00'  # separator before texture section

    # Texture list: chunk_id(1) + name\0
    buf += struct.pack("<I", nc)
    for ci, ch in enumerate(chunks):
        mi = ch['mat']
        tex_name = _v3m_tex_name(obj, mi)
        buf += bytes([ci]) + tex_name.encode('latin-1')[:63] + b'\x00'

    # Material detail: diffuse_name(32) + 4 floats(16) + refl_map(32) + flags(4) = 84 bytes
    buf += struct.pack("<I", nc)
    for ch in chunks:
        mi    = ch['mat']
        flags = obj.get(f'rf_mat_{mi}_flags', MAT_BASE)
        mat_name = _v3m_tex_name(obj, mi)
        buf += mat_name.encode('latin-1')[:31].ljust(32, b'\x00')  # diffuse name
        buf += struct.pack("<4f", 0.0, 0.0, 0.0, 0.0)             # emissive/specular/gloss/refl
        buf += b'\x00' * 32                                         # reflection map (empty)
        buf += struct.pack("<I", flags)

    # Unknown1 section: Redux always writes 1 entry = name(24) + float(4)
    # Stock files have this too. The name is the submesh name.
    buf += struct.pack("<I", 1)
    buf += name.encode('latin-1')[:23].ljust(24, b'\x00')
    buf += struct.pack("<f", 0.0)

    return bytes(buf)


def _build_data_block(chunks, chunk_info):
    nc  = len(chunks)
    db  = bytearray()

    # 56-byte inner chunk headers: 0x20 zeros + texIdx(i32) + 0x14 zeros
    # The engine reads texIdx at offset 0x20 to map chunks to textures
    for ci, info in enumerate(chunk_info):
        db += b'\x00' * 0x20                     # 32 bytes padding
        db += struct.pack("<i", ci)              # texture index for this chunk
        db += b'\x00' * 0x14                     # 20 bytes padding

    # Align inner headers block to 16 bytes
    _db_align16(db)

    # Per-chunk geometry: pos, norm, uv, tri, planes, same_pos — each 16-aligned
    for ch, info in zip(chunks, chunk_info):
        cv = info['cv']
        cf = info['cf']

        # Positions (cv * 12 bytes, padded to 16)
        for x, y, z in ch['verts']:
            db += struct.pack("<3f", x, y, z)
        _db_align16(db)

        # Normals (cv * 12 bytes, padded to 16) — zeros for static meshes
        db += b'\x00' * (cv * 12)
        _db_align16(db)

        # UVs (cv * 8 bytes, padded to 16)
        for u, v in ch['uvs']:
            db += struct.pack("<2f", u, v)
        _db_align16(db)

        # Triangles (cf * 8 bytes: 3 × uint16 indices + uint16 flags, padded to 16)
        # Winding reversed (a,c,b) because bl_to_rf has det=-1 (reflection).
        # Per-tri flag 0x02 matches Redux convention.
        for a, b, c in ch['tris']:
            db += struct.pack("<4H", a, c, b, 0x02)
        _db_align16(db)

        # Triangle planes (cf * 16 bytes: normal(3f) + dist(f), padded to 16)
        # Required because LOD flags include V3D_LOD_TRIANGLE_PLANES (0x20)
        for a_idx, b_idx, c_idx in ch['tris']:
            va = ch['verts'][a_idx]
            vb = ch['verts'][c_idx]   # swapped to match reversed winding
            vc = ch['verts'][b_idx]
            # cross product for face normal
            e1 = (vb[0]-va[0], vb[1]-va[1], vb[2]-va[2])
            e2 = (vc[0]-va[0], vc[1]-va[1], vc[2]-va[2])
            nx = e1[1]*e2[2] - e1[2]*e2[1]
            ny = e1[2]*e2[0] - e1[0]*e2[2]
            nz = e1[0]*e2[1] - e1[1]*e2[0]
            mag = (nx*nx + ny*ny + nz*nz)**0.5
            if mag > 1e-8:
                nx /= mag; ny /= mag; nz /= mag
            else:
                nx, ny, nz = 0.0, 0.0, 1.0
            dist = -(nx*va[0] + ny*va[1] + nz*va[2])
            db += struct.pack("<4f", nx, ny, nz, dist)
        _db_align16(db)

        # Same-position vertex offsets (cv * int16, all zeros, padded to 16)
        db += b'\x00' * (cv * 2)
        _db_align16(db)

        # Weight data (cv * 8 bytes, all zeros for static V3M — stock files include this)
        db += b'\x00' * (cv * 8)
        _db_align16(db)

    return bytes(db)


def _db_align16(db):
    """Pad bytearray to 16-byte alignment."""
    pad = (16 - (len(db) % 16)) % 16
    if pad:
        db += b'\x00' * pad


def _encode_csph(empty):
    buf = bytearray()
    cs_name = empty.name[3:] if empty.name.startswith("CS_") else empty.name
    buf += cs_name.encode('latin-1')[:23].ljust(24, b'\x00')
    buf += struct.pack("<i", -1)
    rx, ry, rz = bl_to_rf(empty.location)
    buf += struct.pack("<3f", rx, ry, rz)
    buf += struct.pack("<f",  empty.empty_display_size)
    return bytes(buf)


# ─────────────────────────────────────────────────────────────────────────────
#  Operators
# ─────────────────────────────────────────────────────────────────────────────

class RFSTATIC_OT_Import(bpy.types.Operator, ImportHelper):
    """Import RF V3M static mesh"""
    bl_idname  = "import_scene.rf_v3m"
    bl_label   = "Import RF V3M"
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".v3m"
    # Blender 5.0+ requires explicit filepath declaration on ImportHelper
    # subclasses; older versions inherit it but explicit is safer.
    filepath: StringProperty(subtype='FILE_PATH')
    filter_glob: StringProperty(default="*.v3m", options={'HIDDEN'})

    import_lod: EnumProperty(
        name="LOD",
        items=[('0',"High (LOD 0)",""),('1',"Medium (LOD 1)",""),('2',"Low (LOD 2)","")],
        default='0'
    )
    import_prop_points: BoolProperty(name="Import Prop Points",       default=True)
    import_cspheres:    BoolProperty(name="Import Collision Spheres",  default=True)
    import_all_subm:    BoolProperty(name="Import All Submeshes",      default=True)

    def execute(self, context):
        try:
            v3m = _parse_v3m(self.filepath)
        except Exception as e:
            self.report({'ERROR'}, f"Parse failed: {e}")
            return {'CANCELLED'}

        if not v3m['submeshes']:
            self.report({'ERROR'}, "No submeshes found")
            return {'CANCELLED'}

        lod_idx  = int(self.import_lod)
        sm_range = range(len(v3m['submeshes'])) if self.import_all_subm else [0]

        root_obj = None
        for si in sm_range:
            try:
                obj = _build_mesh(v3m, lod_idx, si)
            except Exception as e:
                self.report({'WARNING'}, f"Submesh {si} failed: {e}")
                continue
            context.scene.collection.objects.link(obj)
            if root_obj is None:
                root_obj = obj
                if self.import_prop_points or self.import_cspheres:
                    _spawn_empties(v3m, obj, context, si)

        if root_obj:
            context.view_layer.objects.active = root_obj

        n_sm = len(v3m['submeshes'])
        n_pp = sum(len(s['prop_points']) for s in v3m['submeshes'])
        n_cs = len(v3m['cspheres'])
        self.report({'INFO'},
            f"Imported {n_sm} submesh(es), {n_pp} prop point(s), {n_cs} csphere(s)")
        return {'FINISHED'}


class RFSTATIC_OT_Export(bpy.types.Operator, ExportHelper):
    """Export selected mesh(es) as RF V3M static mesh"""
    bl_idname  = "export_scene.rf_v3m"
    bl_label   = "Export RF V3M"
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".v3m"
    filepath: StringProperty(subtype='FILE_PATH')
    filter_glob: StringProperty(default="*.v3m", options={'HIDDEN'})

    export_selected: BoolProperty(name="Selected Only", default=True)
    auto_split: BoolProperty(
        name="Auto-Split Large Meshes",
        description=f"Split meshes exceeding {CHUNK_VERT_LIMIT} verts or {CHUNK_TRI_LIMIT} tris",
        default=True
    )

    def execute(self, context):
        if self.export_selected:
            objects = [o for o in context.selected_objects if o.type == 'MESH']
        else:
            objects = [o for o in context.scene.objects if o.type == 'MESH']

        if not objects:
            self.report({'ERROR'}, "No mesh objects to export")
            return {'CANCELLED'}

        if not self.auto_split:
            for obj in objects:
                obj.data.calc_loop_triangles()
                nv = len(obj.data.vertices)
                nf = len(obj.data.loop_triangles)
                if nv > CHUNK_VERT_LIMIT or nf > CHUNK_TRI_LIMIT:
                    self.report({'WARNING'},
                        f"'{obj.name}' exceeds limits ({nv} verts/{nf} tris). Enable Auto-Split.")
                    return {'CANCELLED'}

        try:
            n_subm = _write_v3m(self.filepath, objects)
        except Exception as e:
            import traceback
            self.report({'ERROR'}, f"Export failed: {e}\n{traceback.format_exc()}")
            return {'CANCELLED'}

        self.report({'INFO'},
            f"Exported {n_subm} submesh(es) → {os.path.basename(self.filepath)}")
        return {'FINISHED'}


class RFSTATIC_OT_AddPropPoint(bpy.types.Operator):
    """Add a prop point empty parented to the active mesh"""
    bl_idname  = "rfstatic.add_prop_point"
    bl_label   = "Add Prop Point"
    bl_options = {'REGISTER', 'UNDO'}

    pp_name: StringProperty(name="Name", default="grip_1")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        obj = context.active_object
        e   = bpy.data.objects.new("PP_" + self.pp_name, None)
        e.empty_display_type = 'ARROWS'
        e.empty_display_size = 0.05
        if obj:
            e.parent   = obj
            e.location = obj.location.copy()
        context.scene.collection.objects.link(e)
        return {'FINISHED'}


class RFSTATIC_OT_AddCSphere(bpy.types.Operator):
    """Add a collision sphere empty parented to the active mesh"""
    bl_idname  = "rfstatic.add_csphere"
    bl_label   = "Add Collision Sphere"
    bl_options = {'REGISTER', 'UNDO'}

    cs_name:   StringProperty(name="Name",   default="body")
    cs_radius: FloatProperty(name="Radius",  default=0.3, min=0.001)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        obj = context.active_object
        e   = bpy.data.objects.new("CS_" + self.cs_name, None)
        e.empty_display_type = 'SPHERE'
        e.empty_display_size = self.cs_radius
        if obj:
            e.parent   = obj
            e.location = obj.location.copy()
        context.scene.collection.objects.link(e)
        return {'FINISHED'}


class RFSTATIC_OT_SetMatFlags(bpy.types.Operator):
    """Set RF texture name and material flags on the active object"""
    bl_idname  = "rfstatic.set_mat_flags"
    bl_label   = "Set RF Material"
    bl_options = {'REGISTER', 'UNDO'}

    mat_index:  IntProperty(name="Material Slot", default=0, min=0)
    tex_name:   StringProperty(
        name="Texture",
        description="Texture filename to write on export (e.g. wood01.tga). "
                    "The material slot is renamed to match (extension stripped).",
        default="",
    )
    flag_alpha: BoolProperty(name="Alpha (0x08)",      default=False)
    flag_full:  BoolProperty(name="Fullbright (0x10)", default=False)

    def invoke(self, context, event):
        obj = context.active_object
        if obj and obj.type == 'MESH':
            flags = obj.get(f'rf_mat_{self.mat_index}_flags', MAT_BASE)
            self.flag_alpha = bool(flags & MAT_ALPHA)
            self.flag_full  = bool(flags & MAT_FULLBRIGHT)
            # Pre-fill texture name from the custom prop, falling back to mat name
            tex = obj.get(f'rf_mat_{self.mat_index}_name', '')
            if not tex and self.mat_index < len(obj.data.materials) and obj.data.materials[self.mat_index]:
                tex = obj.data.materials[self.mat_index].name
            self.tex_name = tex
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            return {'CANCELLED'}
        mi    = self.mat_index
        flags = MAT_BASE
        if self.flag_alpha: flags |= MAT_ALPHA
        if self.flag_full:  flags |= MAT_FULLBRIGHT
        obj[f'rf_mat_{mi}_flags'] = flags

        if mi < len(obj.data.materials) and obj.data.materials[mi]:
            mat = obj.data.materials[mi]
            mat.blend_method = 'BLEND' if self.flag_alpha else 'OPAQUE'

            # Texture name handling: store the full name (with extension) on the
            # custom prop for export, and rename the material slot to the stem
            # so the Blender outliner stays clean (Blender uses dots for the
            # .001 suffix system, so storing 'wood01.tga' as a material name
            # confuses the auto-suffix logic).
            tex = self.tex_name.strip()
            if tex:
                # Auto-append .tga if no recognized extension
                if not tex.lower().endswith(('.tga', '.dds', '.bmp', '.pcx')):
                    tex_full = tex + '.tga'
                else:
                    tex_full = tex
                obj[f'rf_mat_{mi}_name'] = tex_full

                # Material name = stem (strip the extension)
                stem = tex_full.rsplit('.', 1)[0]
                if stem and mat.name != stem:
                    mat.name = stem
        return {'FINISHED'}


# ─────────────────────────────────────────────────────────────────────────────
#  Sidebar panel
# ─────────────────────────────────────────────────────────────────────────────

class RFSTATIC_PT_Panel(bpy.types.Panel):
    bl_label       = "RF Static Mesh"
    bl_idname      = "RFSTATIC_PT_panel"
    bl_space_type  = "VIEW_3D"
    bl_region_type = "UI"
    bl_category    = "RF Static"

    def draw(self, context):
        layout = self.layout
        obj    = context.active_object

        row = layout.row(align=True)
        row.operator("import_scene.rf_v3m", text="Import V3M", icon='IMPORT')
        row.operator("export_scene.rf_v3m", text="Export V3M", icon='EXPORT')

        if not obj or obj.type != 'MESH':
            return

        layout.separator()
        box = layout.box()
        box.label(text="Mesh Info", icon='MESH_DATA')
        mesh = obj.data
        mesh.calc_loop_triangles()
        nv = len(mesh.vertices)
        nf = len(mesh.loop_triangles)

        for label, count, limit in [("Verts", nv, CHUNK_VERT_LIMIT),
                                     ("Tris",  nf, CHUNK_TRI_LIMIT)]:
            row = box.row()
            row.label(text=f"{label}: {count}")
            pct = count / limit
            if pct > 1.0:
                row.label(text="OVER LIMIT", icon='ERROR')
            elif pct > 0.8:
                row.label(text=f"{pct*100:.0f}%", icon='INFO')

        if nv > CHUNK_VERT_LIMIT or nf > CHUNK_TRI_LIMIT:
            est = max(math.ceil(nv/CHUNK_VERT_LIMIT), math.ceil(nf/CHUNK_TRI_LIMIT))
            box.label(text=f"→ ~{est} submeshes on export", icon='MOD_EXPLODE')

        layout.separator()
        box = layout.box()
        box.label(text="Materials", icon='MATERIAL')
        for mi, mat in enumerate(obj.data.materials):
            if mat:
                flags = obj.get(f'rf_mat_{mi}_flags', MAT_BASE)
                tag = ("A" if flags & MAT_ALPHA else "") + ("F" if flags & MAT_FULLBRIGHT else "")
                tex = obj.get(f'rf_mat_{mi}_name', '') or (mat.name + '.tga')
                row = box.row(align=True)
                row.label(text=f"[{mi}]")
                row.label(text=tex[:24], icon='TEXTURE')
                if tag:
                    row.label(text=tag)
                op = row.operator("rfstatic.set_mat_flags", text="", icon='SETTINGS')
                op.mat_index = mi

        row2 = layout.row(align=True)
        row2.operator("export_scene.rf_rfg", text="Export RFG", icon='EXPORT')

        layout.separator()
        box = layout.box()
        box.label(text="Prop Points & Collision", icon='EMPTY_ARROWS')
        box.operator("rfstatic.add_prop_point", text="Add Prop Point", icon='ADD')
        box.operator("rfstatic.add_csphere",    text="Add Csphere",    icon='SPHERE')

        children = obj.children if obj else []
        pps = [c for c in children if c.name.startswith("PP_")]
        css = [c for c in children if c.name.startswith("CS_")]
        if pps:
            col = box.column()
            col.label(text=f"Prop Points ({len(pps)}):")
            for pp in pps:
                col.label(text=f"  {pp.name}", icon='EMPTY_ARROWS')
        if css:
            col = box.column()
            col.label(text=f"Collision Spheres ({len(css)}):")
            for cs in css:
                col.label(text=f"  {cs.name} r={cs.empty_display_size:.3f}", icon='SPHERE')


# ─────────────────────────────────────────────────────────────────────────────
#  RFG (Red Faction Group) Writer — matches Redux RfgExporter output (ver 0x12C)
# ─────────────────────────────────────────────────────────────────────────────
#
#  Format verified against Redux's RfgExporter.cs + RfgParser.cs +
#  RFGeometryParser.cs (the canonical Alpine Faction tool):
#
#  FILE HEADER:
#     magic        u32   0xD43DD00D
#     version      i32   0x0000012C   (300 — Alpine Faction RF1 format)
#     num_groups   i32   1            (one static group)
#
#  PER GROUP:
#     name         VString
#     is_moving    u8    0
#     num_brushes  i32
#     brushes[]    ...
#     21 trailing section counts (all zero for a geometry-only group)
#
#  PER BRUSH:
#     uid          i32
#     position     3f
#     forward      3f   ← rotation rows in (fwd, right, up) order
#     right        3f
#     up           3f
#
#     ── geometry body (version >= 0xC8 layout) ──
#     unk1         i32   0            ← these two ints replace the old
#     unk2         i32   0            ← unk_mod field of pre-0xC8 files
#     geo_name     VString ""
#     num_tex      i32
#     textures[]   VString
#     num_scroll   i32   0            (face scroll table — empty)
#     num_rooms    i32   0
#     num_subroom  i32   0
#     num_portals  i32   0
#     num_verts    i32
#     verts[]      3f
#     num_faces    i32
#     faces[]      see below
#     num_surfaces i32   0
#
#     ── brush footer ──
#     flags        u32
#     life         i32
#     state        i32
#
#  PER FACE (Redux RfgExporter writes the plane data — RED's parser
#  expects it; the previous "RED doesn't include planes" note was wrong):
#     plane_normal 3f
#     plane_dist   f32
#     tex_index    i32
#     surf_idx     i32   -1
#     face_id      i32
#     unk12        i32   -1
#     reserved1    u32   0xFFFFFFFF
#     portal_idx   i32   -1
#     face_flags   u16
#     reserved2    u16   0
#     smooth_grp   u32
#     room_idx     i32   -1
#     num_verts    i32
#     per vert:    i32 index, f32 u, f32 v
#
# ─────────────────────────────────────────────────────────────────────────────

RFG_MAGIC   = 0xD43DD00D
RFG_VERSION = 0x0000012C    # 300 — Alpine Faction RF1; matches Redux RfgExporter

# 21 trailing section counts that RED expects after each group's brushes.
# Each is read by the corresponding RflUtils.Skip*() call in Redux's RfgParser:
#   geo_regions, lights, cutscene_cameras, cutscene_path_nodes, ambient_sounds,
#   events, mp_respawn_points, nav_points, entities, items, clutters, triggers,
#   particle_emitters, gas_regions, decals, climbing_regions, room_effects,
#   eax_effects, bolt_emitters, targets, push_regions
_RFG_TRAILING_SECTIONS = 21


def _rfg_vstring(s):
    """Encode a VString: uint16 length + ASCII bytes (no null terminator)."""
    b = s.encode('latin-1')
    if len(b) > 0xFFFE:
        b = b[:0xFFFE]
    return struct.pack("<H", len(b)) + b


def _rfg_collect_brushes(objects):
    """
    Collect brush data from Blender mesh objects.
    Each material on each object becomes one brush.
    Returns a list of brush dicts.
    """
    brushes = []
    face_id_counter = 0

    for obj in objects:
        mesh = obj.data
        mesh.calc_loop_triangles()
        uv_data = mesh.uv_layers.active.data if mesh.uv_layers.active else None

        # Group triangles by material
        mat_tris = {}
        for lt in mesh.loop_triangles:
            mi = lt.material_index
            mat_tris.setdefault(mi, []).append(lt)

        for mi, tris in mat_tris.items():
            # Texture name
            tex_name = "default.tga"
            if mi < len(mesh.materials) and mesh.materials[mi]:
                mat = mesh.materials[mi]
                tex_name = obj.get(f'rf_mat_{mi}_name', mat.name)
                if not tex_name.lower().endswith(('.tga', '.dds', '.bmp', '.pcx')):
                    tex_name += ".tga"

            # Collect unique verts by Blender vertex index alone.
            # RFG stores UVs per-face-corner (in the face record), not per-vertex,
            # so faces sharing a vertex position should reference the same vertex
            # entry regardless of UV. This makes brushes editable in RED — moving
            # one vertex moves all adjacent faces with it. Keying by (vi, li)
            # would make every face-corner unique, leaving each face as an
            # isolated island.
            vert_map = {}
            local_verts = []
            face_list   = []
            wmat = obj.matrix_world

            for lt in tris:
                face_indices = []
                face_uvs_local = []
                for vi, li in zip(lt.vertices, lt.loops):
                    if vi not in vert_map:
                        vert_map[vi] = len(local_verts)
                        # World-space Blender coords → RF coords
                        world_co = wmat @ mesh.vertices[vi].co
                        local_verts.append(bl_to_rf(world_co))
                    face_indices.append(vert_map[vi])
                    if uv_data:
                        uv = uv_data[li].uv
                        face_uvs_local.append((uv.x, 1.0 - uv.y))
                    else:
                        face_uvs_local.append((0.0, 0.0))

                # Reverse winding: bl_to_rf has det=-1, flips face orientation.
                # After reversal, CCW (in RF) gives outward normals via the
                # standard cross product b-a × c-a (computed below per face).
                face_indices.reverse()
                face_uvs_local.reverse()

                face_list.append({
                    'indices': face_indices,
                    'uvs':     face_uvs_local,
                    'face_id': face_id_counter,
                })
                face_id_counter += 1

            # Brush position = (0,0,0) since vertices are already in RF world space
            brushes.append({
                'textures':   [tex_name],
                'verts':      local_verts,
                'faces':      face_list,
                'position':   (0.0, 0.0, 0.0),
            })

    return brushes


def _rfg_face_plane(verts, indices):
    """
    Compute the plane (normal, dist) for a polygon defined by `indices` into
    `verts` (list of (x,y,z) tuples). Uses the Newell-style accumulated cross
    product to be robust on small/degenerate polys, then plane dist d such that
    dot(n, p) + d = 0 for any p on the plane.

    Matches Redux's RfgExporter.WriteBrushesSection plane computation.
    """
    if len(indices) < 3:
        return (0.0, 0.0, 1.0, 0.0)
    ax, ay, az = verts[indices[0]]
    nx = ny = nz = 0.0
    for i in range(1, len(indices) - 1):
        bx, by, bz = verts[indices[i]]
        cx, cy, cz = verts[indices[i + 1]]
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        nx += uy * vz - uz * vy
        ny += uz * vx - ux * vz
        nz += ux * vy - uy * vx
    length = math.sqrt(nx * nx + ny * ny + nz * nz)
    if length < 1e-12:
        return (0.0, 0.0, 1.0, 0.0)
    nx /= length; ny /= length; nz /= length
    dist = -(nx * ax + ny * ay + nz * az)
    return (nx, ny, nz, dist)


def _rfg_encode_brush(brush, uid):
    """Encode a single brush to bytes, matching Redux's on-disk layout for
    version 0x12C (Alpine Faction RF1)."""
    buf = bytearray()

    # UID
    buf += struct.pack("<i", uid)

    # Position (3 floats)
    buf += struct.pack("<3f", *brush['position'])

    # Rotation matrix — written as (forward, right, up) row vectors.
    # Identity in RF (Y-up, Z-fwd, X-right):
    #   fwd=(0,0,1), right=(1,0,0), up=(0,1,0)
    buf += struct.pack("<9f", 0, 0, 1,   1, 0, 0,   0, 1, 0)

    # ── Geometry body (version >= 0xC8 layout) ──
    # 8 bytes of zero before the name (was 4-byte unk_mod in older versions)
    buf += struct.pack("<II", 0, 0)

    # Geo name (empty)
    buf += _rfg_vstring("")

    # Textures
    textures = brush['textures']
    buf += struct.pack("<i", len(textures))
    for tex in textures:
        buf += _rfg_vstring(tex)

    # Face scroll table (empty)
    buf += struct.pack("<i", 0)
    # Rooms / subroom links / portals (all empty)
    buf += struct.pack("<iii", 0, 0, 0)

    # Vertices
    verts = brush['verts']
    buf += struct.pack("<i", len(verts))
    for x, y, z in verts:
        buf += struct.pack("<3f", x, y, z)

    # Faces — each face has plane data BEFORE texture index (per Redux)
    faces = brush['faces']
    buf += struct.pack("<i", len(faces))
    for face in faces:
        indices = face['indices']
        uvs     = face['uvs']

        # Plane: normal (3f) + dist (f32) — computed from the (already
        # winding-reversed) indices so the normal points outward in RF space.
        nx, ny, nz, dist = _rfg_face_plane(verts, indices)
        buf += struct.pack("<4f", nx, ny, nz, dist)

        tex_index = 0                  # index into this brush's texture list
        surf_idx  = -1
        face_id   = face['face_id']
        unk12     = -1
        reserved1 = 0xFFFFFFFF
        portal    = -1
        fflags    = 0                  # uint16
        reserved2 = 0                  # uint16
        smooth    = 0                  # smoothing groups (uint32)
        room      = -1

        buf += struct.pack("<i", tex_index)
        buf += struct.pack("<i", surf_idx)
        buf += struct.pack("<i", face_id)
        buf += struct.pack("<i", unk12)
        buf += struct.pack("<I", reserved1)
        buf += struct.pack("<i", portal)
        buf += struct.pack("<H", fflags)
        buf += struct.pack("<H", reserved2)
        buf += struct.pack("<I", smooth)
        buf += struct.pack("<i", room)

        buf += struct.pack("<i", len(indices))
        for vi_idx, (u, v) in zip(indices, uvs):
            buf += struct.pack("<i", vi_idx)
            buf += struct.pack("<2f", u, v)

    # Surfaces (count = 0). For version > 0xB4 there's NO old-face-scroll
    # block after this, so brush footer follows immediately.
    buf += struct.pack("<i", 0)

    # Brush flags, life, state
    buf += struct.pack("<I", 0)         # flags
    buf += struct.pack("<i", 4)         # life (stock uses 4)
    buf += struct.pack("<i", 0)         # state

    return bytes(buf)


def _write_rfg(filepath, objects, group_name=""):
    """
    Write a .rfg group file from Blender mesh objects.
    Each material on each object becomes one brush.
    Format matches Redux's RfgExporter output (RF1 version 0x12C / 300).
    """
    if not group_name:
        group_name = os.path.splitext(os.path.basename(filepath))[0]

    brushes = _rfg_collect_brushes(objects)
    if not brushes:
        raise ValueError("No geometry to export")

    buf = bytearray()

    # ── File header ──
    buf += struct.pack("<I", RFG_MAGIC)
    buf += struct.pack("<i", RFG_VERSION)
    buf += struct.pack("<i", 1)         # num_groups = 1

    # ── Group header ──
    buf += _rfg_vstring(group_name)
    buf += struct.pack("<B", 0)         # is_moving = false

    # ── Brushes ──
    buf += struct.pack("<i", len(brushes))
    for i, brush in enumerate(brushes):
        uid = i + 1                     # UIDs start at 1
        buf += _rfg_encode_brush(brush, uid)

    # ── 21 trailing section counts (all zero — geometry-only group) ──
    for _ in range(_RFG_TRAILING_SECTIONS):
        buf += struct.pack("<i", 0)

    with open(filepath, 'wb') as f:
        f.write(buf)

    return len(brushes)


# ─────────────────────────────────────────────────────────────────────────────
#  RFG Export Operator
# ─────────────────────────────────────────────────────────────────────────────

class RFSTATIC_OT_ExportRFG(bpy.types.Operator, ExportHelper):
    """Export selected mesh(es) as RF Group (.rfg) for RED editor"""
    bl_idname  = "export_scene.rf_rfg"
    bl_label   = "Export RF Group"
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".rfg"
    filepath: StringProperty(subtype='FILE_PATH')
    filter_glob: StringProperty(default="*.rfg", options={'HIDDEN'})

    export_selected: BoolProperty(
        name="Selected Only",
        description="Export only selected mesh objects",
        default=True
    )
    group_name: StringProperty(
        name="Group Name",
        description="Name for the RFG group (defaults to filename)",
        default=""
    )

    def execute(self, context):
        if self.export_selected:
            objects = [o for o in context.selected_objects if o.type == 'MESH']
        else:
            objects = [o for o in context.scene.objects if o.type == 'MESH']

        if not objects:
            self.report({'ERROR'}, "No mesh objects to export")
            return {'CANCELLED'}

        gname = self.group_name.strip() or ""
        try:
            n = _write_rfg(self.filepath, objects, gname)
        except Exception as e:
            import traceback
            self.report({'ERROR'}, f"RFG export failed: {e}\n{traceback.format_exc()}")
            return {'CANCELLED'}

        self.report({'INFO'},
            f"Exported {n} brush(es) → {os.path.basename(self.filepath)}")
        return {'FINISHED'}




def menu_import(self, context):
    self.layout.operator("import_scene.rf_v3m", text="RF Static Mesh (.v3m)")

def menu_export(self, context):
    self.layout.operator("export_scene.rf_v3m", text="RF Static Mesh (.v3m)")
    self.layout.operator("export_scene.rf_rfg", text="RF Group (.rfg)")


classes = [
    RFSTATIC_OT_Import, RFSTATIC_OT_Export, RFSTATIC_OT_ExportRFG,
    RFSTATIC_OT_AddPropPoint, RFSTATIC_OT_AddCSphere,
    RFSTATIC_OT_SetMatFlags, RFSTATIC_PT_Panel,
]


def register():
    failed = []
    for cls in classes:
        try:
            bpy.utils.register_class(cls)
        except Exception as e:
            failed.append((cls.__name__, str(e)))
    try:
        bpy.types.TOPBAR_MT_file_import.append(menu_import)
    except Exception as e:
        failed.append(("menu_import", str(e)))
    try:
        bpy.types.TOPBAR_MT_file_export.append(menu_export)
    except Exception as e:
        failed.append(("menu_export", str(e)))
    if failed:
        print("[RF Static Mesh Tools] register: partial failure:")
        for name, err in failed:
            print(f"  - {name}: {err}")


def unregister():
    failed = []
    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception as e:
            failed.append((cls.__name__, str(e)))
    try:
        bpy.types.TOPBAR_MT_file_import.remove(menu_import)
    except Exception as e:
        failed.append(("menu_import", str(e)))
    try:
        bpy.types.TOPBAR_MT_file_export.remove(menu_export)
    except Exception as e:
        failed.append(("menu_export", str(e)))
    if failed:
        print("[RF Static Mesh Tools] unregister: partial failure:")
        for name, err in failed:
            print(f"  - {name}: {err}")


if __name__ == "__main__":
    register()

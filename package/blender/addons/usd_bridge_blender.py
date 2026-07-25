"""
usd_bridge_blender - Blender side of the usd_bridge (Houdini 22 / ZBrush 2026 / Blender 5.2).

Two jobs:

1. Serve Houdini. A background timer watches <cache>/blender/cmd.json. Houdini's
   "Send to Blender" / "Get from Blender" shelf tools write a command; this add-on
   imports/exports USD and answers via done.json. Transport is pure USD, so
   materials, UVs and colour attributes travel natively both ways.

2. Talk to ZBrush directly (no Houdini needed): "Get from ZBrush" / "Send to ZBrush"
   in View3D > Sidebar > USD Portal. Same proven ZScript machinery as the Houdini
   package, but over OBJ (Blender reads/writes it natively, ZBrush's Tool:Import/
   Export handles it, and the vertex-colour + #MRGB extensions carry polypaint).

Install: Edit > Preferences > Add-ons > Install from Disk -> this file (or add
`package/blender` as a Script Directory to load it live from the repo).
"""

bl_info = {
    "name": "USD Portal (Houdini / ZBrush / Maya)",
    "author": "Eugene Fokin",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > USD Portal",
    "description": "USD Portal to Houdini (via cache commands) and ZBrush (via ZScript)",
    "category": "Import-Export",
}

import glob
import json
import os
import re
import subprocess
import time
import uuid
from shutil import copyfile, rmtree

import bpy
import numpy as np

# --------------------------------------------------------------------------- #
# Config (defaults match the Houdini package; override in add-on preferences)
# --------------------------------------------------------------------------- #
DEFAULT_CACHE = os.getenv("USD_BRIDGE_CACHE", "C:/usd_bridge_cache")
DEFAULT_ZBRUSH = os.getenv("ZBRUSH_EXEC_PATH", "C:/Program Files/Maxon ZBrush 2026/ZBrush.exe")

GET_SIGNAL = "ub_blget_done"      # ZBrush -> Blender export finished
SEND_SIGNAL = "ub_blsend_done"    # Blender -> ZBrush import finished
SIGNAL_TIMEOUT = 180.0

GET_FLIP_TEXTURE_V = True         # ZBrush OBJ UVs are V-flipped vs its Texture Map
SEND_FLIP_TEXTURE_V = True        # same mismatch on the way in (see Houdini module)
SEND_TO_CURRENT_TOOL = False      # False = new tool via PolyMesh3D (robust)


def _prefs():
    try:
        return bpy.context.preferences.addons[__name__].preferences
    except (KeyError, AttributeError):
        return None


def _cache_dir():
    p = _prefs()
    return (p.cache_dir if p and p.cache_dir else DEFAULT_CACHE).replace("\\", "/").rstrip("/")


def _zbrush_exe():
    p = _prefs()
    return (p.zbrush_exe if p and p.zbrush_exe else DEFAULT_ZBRUSH).replace("\\", "/")


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _ensure_dir(path):
    if not os.path.isdir(path):
        os.makedirs(path)


def _reset_dir(path):
    if os.path.isdir(path):
        rmtree(path, ignore_errors=True)
    os.makedirs(path)


def _fs_name(name):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name).strip("_") or "mesh"


def _kw(op, **want):
    """Filter kwargs to what this Blender build's operator actually has."""
    props = op.get_rna_type().properties.keys()
    return {k: v for k, v in want.items() if k in props}


def _atomic_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def _flip_image_v(src, dst):
    """Write a vertically flipped copy of an image (raw pixels, no colour transform)."""
    img = None
    out = None
    try:
        img = bpy.data.images.load(src)
        img.colorspace_settings.name = "Non-Color"      # passthrough, no double-encode
        w, h = img.size
        px = np.empty(w * h * 4, dtype=np.float32)
        img.pixels.foreach_get(px)
        px = px.reshape(h, w, 4)[::-1].reshape(-1)
        out = bpy.data.images.new("ub_flip_tmp", width=w, height=h, alpha=True)
        out.colorspace_settings.name = "Non-Color"
        out.pixels.foreach_set(px)
        out.filepath_raw = dst
        out.file_format = "PNG"
        out.save()
        return True
    except Exception:
        try:
            copyfile(src, dst)
            return True
        except OSError:
            return False
    finally:
        for im in (img, out):
            if im is not None:
                try:
                    bpy.data.images.remove(im)
                except Exception:
                    pass


# --------------------------------------------------------------------------- #
# ZBrush signalling (same sandbox-aware scheme as the Houdini package)
# --------------------------------------------------------------------------- #
def _signal_paths(name):
    filename = name + ".zvr"
    paths = [os.path.join(_cache_dir(), filename)]
    for base in (os.getenv("LOCALAPPDATA"), os.getenv("TEMP"), os.getenv("TMP")):
        if not base:
            continue
        for pattern in (os.path.join(base, "Temp", "ZBrushData*"),
                        os.path.join(base, "ZBrushData*")):
            for folder in glob.glob(pattern):
                paths.append(os.path.join(folder, filename))
    return paths


def _clear_signal(name):
    for p in _signal_paths(name):
        try:
            if os.path.isfile(p):
                os.remove(p)
        except OSError:
            pass


def _signal_found(name):
    return any(os.path.isfile(p) for p in _signal_paths(name))


def _run_zscript(text, script_path):
    _ensure_dir(os.path.dirname(script_path))
    with open(script_path, "w") as fh:
        fh.write(text)
    subprocess.Popen([_zbrush_exe(), script_path])


# --------------------------------------------------------------------------- #
# ZScript templates (ported from the Houdini package; OBJ instead of GoZ)
# --------------------------------------------------------------------------- #
_ZS_GET = r"""[If, 1,
[RoutineDef, ActiveSubToolVisible,
    [VarSet, result, 0]
    [VarSet, idx, [SubToolGetActiveIndex]]
    [VarSet, st, [SubToolGetStatus, idx]]
    [VarSet, folder, [SubToolGetFolderIndex, idx]]
    [If, (folder > -1),
        [VarSet, fst, [SubToolGetStatus, folder]]
        [If, (([Val, fst] & 0x2) == 0x2) && (([Val, st] & 0x1) == 0x1),
            [VarSet, result, 1]]
    ,
        [If, (([Val, st] & 0x1) == 0x1),
            [VarSet, result, 1]]
    ]
, result]
[IFreeze,
    [VarSet, restoreIndex, [SubToolGetActiveIndex]]
    [VarSet, total, [SubToolGetCount]]
    [VarSet, i, 0]
    [Loop, total,
        [SubToolSelect, i]
        [VarSet, vis, 0]
        [RoutineCall, ActiveSubToolVisible, vis]
        [If, vis,
            [VarSet, nm, [IGetTitle, Tool:Subtool:ItemInfo]]
            [VarSet, nmLen, [StrLength, nm]]
            [VarSet, nm, [StrExtract, nm, 0, nmLen - 2]]
            [FileNameSetNext, [StrMerge, "__EXPORT_DIR__",
                [StrMerge, [StrMerge, [StrMerge, i, "_"], nm], ".obj"]]]
            [IPress, Tool:Export]
            [If, [IsEnabled, Tool:Texture Map:Clone Txtr],
                [IPress, Tool:Texture Map:Clone Txtr]
                [FileNameSetNext, [StrMerge, "__TEX_DIR__",
                    [StrMerge, [StrMerge, [StrMerge, i, "_"], nm], ".png"]]]
                [IPress, Texture:Export]
            ]
        ]
        [VarInc, i]
    ]
    [SubToolSelect, restoreIndex]
]
[VarSet, done, 0]
[VarSave, done, __SIGNAL__]
]"""

_ZS_SEND = r"""[If, 1,
[VarDef, count, __COUNT__]
[VarDef, files(count), ""]
[VarDef, texfiles(count), ""]
[VarDef, texnames(count), ""]
__FILE_LIST__
[IFreeze,
    [If, (__APPEND__ && ([SubToolGetCount] > 0)),
        [If, ([IGet, Transform:Edit] == 0), [IPress, Transform:Edit]]
        [VarSet, k, 0]
        [Loop, count,
            [IPress, Tool:SubTool:Duplicate]
            [FileNameSetNext, [Var, files(k)]]
            [IPress, Tool:Import]
            __TEX_K__
            [VarInc, k]
        ]
    ,
        [IPress, Tool:PolyMesh3D]
        [FileNameSetNext, [Var, files(0)]]
        [IPress, Tool:Import]
        [If, ([IGet, Transform:Edit] == 0),
            [CanvasClick, 10, 10, 10, 20]
            [IPress, Transform:Edit]
        ]
        __TEX_0__
        [VarSet, k, 1]
        [Loop, count - 1,
            [IPress, Tool:SubTool:Duplicate]
            [FileNameSetNext, [Var, files(k)]]
            [IPress, Tool:Import]
            __TEX_K__
            [VarInc, k]
        ]
    ]
]
[VarSet, done, 0]
[VarSave, done, __SIGNAL__]
]"""

_ZS_TEX_APPLY = """[If, ([StrLength, [Var, texnames(INDEX)]] > 0),
                [FileNameSetNext, [Var, texfiles(INDEX)]]
                [IPress, Texture:Import]
                [IPress, Tool:Texture Map:TextureMap]
                [IPress, [StrMerge, "PopUp:", [Var, texnames(INDEX)]]]
            ]"""


# --------------------------------------------------------------------------- #
# Houdini command server (cmd.json -> done.json)
# --------------------------------------------------------------------------- #
def _blender_exchange_dir():
    return _cache_dir() + "/blender"


def _process_command(cmd):
    """Execute one Houdini command. Returns the done payload (without ok/id)."""
    op = cmd.get("op")
    if op == "import_usd":
        usd = cmd["usd"]
        before = {o.name for o in bpy.data.objects}
        bpy.ops.wm.usd_import(**_kw(bpy.ops.wm.usd_import,
                                    filepath=usd,
                                    import_usd_preview=True,
                                    read_mesh_colors=True,
                                    read_mesh_uvs=True,
                                    validate_meshes=True))
        new = [o for o in bpy.data.objects if o.name not in before]
        for obj in new:                      # Cd-only meshes: make the colour visible
            if obj.type == "MESH" and not _has_real_material(obj):
                _material_with_vertex_color(obj, obj.name)
        return {"objects": len(new), "names": [o.name for o in new][:50]}

    if op == "export_usd":
        usd = cmd["usd"]
        _ensure_dir(os.path.dirname(usd))
        # Hidden objects never export: the pool is visible meshes only, narrowed to
        # the (visible) selection when there is one.
        meshes = [o for o in bpy.data.objects if o.type == "MESH" and o.visible_get()]
        selected = [o for o in meshes if o.select_get()]
        targets = selected or meshes
        if not targets:
            raise ValueError("no visible mesh objects to export")

        prev_sel = [o for o in bpy.data.objects if o.select_get()]
        prev_active = bpy.context.view_layer.objects.active

        # Colour attributes export as primvars under their own name (e.g. "Color"
        # from an OBJ import) and consumers only auto-map displayColor. Temp-rename
        # the active/first colour attribute for the export (restored below).
        renamed = []
        for o in targets:
            attrs = getattr(o.data, "color_attributes", None)
            if not attrs or not len(attrs):
                continue
            if any(a.name == "displayColor" for a in attrs):
                continue
            attr = attrs.active_color if attrs.active_color else attrs[0]
            renamed.append((o.data, attr.name))
            attr.name = "displayColor"

        try:
            bpy.ops.object.select_all(action="DESELECT")
        except Exception:
            pass
        for o in targets:
            o.select_set(True)
        try:
            # Geometry + materials only: no lights/cameras, and no world->DomeLight
            # conversion (Blender's default dark-grey world otherwise ships as an
            # env light that kills Solaris' headlight and shades everything black).
            bpy.ops.wm.usd_export(**_kw(bpy.ops.wm.usd_export,
                                        filepath=usd,
                                        selected_objects_only=True,
                                        export_materials=True,
                                        generate_preview_surface=True,
                                        export_uvmaps=True,
                                        export_mesh_colors=True,
                                        export_normals=True,
                                        export_lights=False,
                                        export_cameras=False,
                                        convert_world_material=False,
                                        convert_orientation=True,
                                        export_global_forward_selection="NEGATIVE_Z",
                                        export_global_up_selection="Y",
                                        relative_paths=False))
        finally:
            for mesh_data, original in renamed:
                try:
                    mesh_data.color_attributes["displayColor"].name = original
                except Exception:
                    pass
            try:
                bpy.ops.object.select_all(action="DESELECT")
                for o in prev_sel:
                    o.select_set(True)
                bpy.context.view_layer.objects.active = prev_active
            except Exception:
                pass
        return {"objects": len(targets)}

    raise ValueError("unknown op: %r" % op)


def _watcher_tick():
    xdir = _blender_exchange_dir()
    cmd_path = xdir + "/cmd.json"
    if os.path.isfile(cmd_path):
        try:
            with open(cmd_path) as fh:
                cmd = json.load(fh)
        except (OSError, ValueError):
            return 0.5                      # half-written; retry next tick
        try:
            os.remove(cmd_path)
        except OSError:
            pass
        try:
            result = _process_command(cmd)
            result["ok"] = 1
        except Exception as exc:            # report the failure to Houdini
            result = {"ok": 0, "error": str(exc)}
        result["id"] = cmd.get("id", "")
        try:
            _atomic_json(xdir + "/done.json", result)
        except OSError:
            pass
    return 0.5


# --------------------------------------------------------------------------- #
# ZBrush -> Blender ("Get from ZBrush")
# --------------------------------------------------------------------------- #
def _get_dirs():
    c = _cache_dir()
    return c + "/bl_get", c + "/bl_get_tex"


def _assign_material(obj, mat):
    if obj.data.materials:
        obj.data.materials[0] = mat
    else:
        obj.data.materials.append(mat)


def _material_with_texture(obj, name, image_path):
    mat = bpy.data.materials.new("UB_" + name)
    mat.use_nodes = True
    bsdf = next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
    tex = mat.node_tree.nodes.new("ShaderNodeTexImage")
    try:
        tex.image = bpy.data.images.load(image_path)
    except Exception:
        pass
    if bsdf is not None:
        tex.location = (bsdf.location.x - 350, bsdf.location.y)
        mat.node_tree.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    _assign_material(obj, mat)


def _material_with_vertex_color(obj, name):
    """For meshes that carry polypaint/Cd but no texture: a material sampling the
    first colour attribute, so the paint shows in material preview and renders."""
    attrs = getattr(obj.data, "color_attributes", None)
    if not attrs or not len(attrs):
        return False
    mat = bpy.data.materials.new("UB_" + name)
    mat.use_nodes = True
    bsdf = next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
    vc = mat.node_tree.nodes.new("ShaderNodeVertexColor")   # "Color Attribute" node
    vc.layer_name = attrs[0].name
    if bsdf is not None:
        vc.location = (bsdf.location.x - 300, bsdf.location.y)
        mat.node_tree.links.new(vc.outputs["Color"], bsdf.inputs["Base Color"])
    _assign_material(obj, mat)
    return True


def _has_real_material(obj):
    """A material counts only if it can actually shade: node materials need a
    connected Material Output (Maya's empty initialShadingGroup imports as a
    node tree with no output - it would render black)."""
    for mat in obj.data.materials:
        if mat is None:
            continue
        if not mat.use_nodes:
            return True
        for node in mat.node_tree.nodes:
            if node.type == "OUTPUT_MATERIAL" and node.inputs["Surface"].links:
                return True
    return False


def _import_zbrush_results():
    obj_dir, tex_dir = _get_dirs()
    files = sorted(glob.glob(obj_dir + "/*.obj"),
                   key=lambda p: (int(re.match(r"(\d+)_", os.path.basename(p)).group(1))
                                  if re.match(r"(\d+)_", os.path.basename(p)) else 0,
                                  os.path.basename(p).lower()))
    imported = []
    for path in files:
        stem = os.path.splitext(os.path.basename(path))[0]
        nice = re.sub(r"^\d+_", "", stem) or "subtool"
        before = {o.name for o in bpy.data.objects}
        try:
            bpy.ops.wm.obj_import(**_kw(bpy.ops.wm.obj_import,
                                        filepath=path,
                                        up_axis="Y",
                                        forward_axis="NEGATIVE_Z",
                                        validate_meshes=True))
        except Exception:
            continue
        new = [o for o in bpy.data.objects if o.name not in before and o.type == "MESH"]
        if not new:
            continue
        target = new[0]
        target.name = nice
        tex = "%s/%s.png" % (tex_dir, stem)
        if os.path.isfile(tex):
            use = tex
            if GET_FLIP_TEXTURE_V:
                flipped = "%s/%s_flip.png" % (tex_dir, stem)
                if _flip_image_v(tex, flipped):
                    use = flipped
            _material_with_texture(target, nice, use)
        else:
            _material_with_vertex_color(target, nice)   # polypaint-only subtool
        imported.append(target)
    return imported


class UB_OT_get_from_zbrush(bpy.types.Operator):
    bl_idname = "usd_bridge.get_from_zbrush"
    bl_label = "Get from ZBrush"
    bl_description = "Export every visible ZBrush subtool (OBJ + texture) and import it here"

    _timer = None
    _deadline = 0.0

    def execute(self, context):
        exe = _zbrush_exe()
        if not os.path.isfile(exe):
            self.report({"ERROR"}, "ZBrush.exe not found: %s" % exe)
            return {"CANCELLED"}
        obj_dir, tex_dir = _get_dirs()
        _reset_dir(obj_dir)
        _reset_dir(tex_dir)
        script = (_ZS_GET
                  .replace("__EXPORT_DIR__", obj_dir + "/")
                  .replace("__TEX_DIR__", tex_dir + "/")
                  .replace("__SIGNAL__", GET_SIGNAL))
        _clear_signal(GET_SIGNAL)
        _run_zscript(script, _cache_dir() + "/bl_get.txt")

        self._deadline = time.time() + SIGNAL_TIMEOUT
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.3, window=context.window)
        wm.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type == "ESC":
            return self._finish(context, cancelled=True)
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        if _signal_found(GET_SIGNAL):
            imported = _import_zbrush_results()
            self.report({"INFO"}, "USD Portal: imported %d subtool(s) from ZBrush"
                        % len(imported))
            return self._finish(context)
        if time.time() > self._deadline:
            self.report({"WARNING"},
                        "ZBrush did not answer in time - is it running with a tool loaded?")
            return self._finish(context, cancelled=True)
        return {"PASS_THROUGH"}

    def _finish(self, context, cancelled=False):
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        return {"CANCELLED"} if cancelled else {"FINISHED"}


# --------------------------------------------------------------------------- #
# Blender -> ZBrush ("Send to ZBrush")
# --------------------------------------------------------------------------- #
def _srgb_encode(c):
    return c * 12.92 if c <= 0.0031308 else 1.055 * (c ** (1.0 / 2.4)) - 0.055


def _encode_obj_vertex_colors(path):
    """ZBrush decodes OBJ vertex colours sRGB->linear; pre-encode 'v x y z r g b'
    lines linear->sRGB so polypaint lands looking right (same trick as Houdini send)."""
    try:
        with open(path) as fh:
            lines = fh.readlines()
    except OSError:
        return
    changed = False
    for i, line in enumerate(lines):
        if not line.startswith("v "):
            continue
        parts = line.split()
        if len(parts) < 7:
            continue
        try:
            rgb = [max(0.0, min(1.0, float(x))) for x in parts[4:7]]
        except ValueError:
            continue
        enc = ["%.6f" % _srgb_encode(c) for c in rgb]
        lines[i] = " ".join(parts[:4] + enc) + "\n"
        changed = True
    if changed:
        with open(path, "w") as fh:
            fh.writelines(lines)


def _texture_of(obj):
    """The base-colour image of the object's active material, or None."""
    mat = obj.active_material
    if not mat or not mat.use_nodes:
        return None
    bsdf = next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
    nodes = []
    if bsdf is not None:
        base = bsdf.inputs.get("Base Color")
        if base and base.links:
            nodes.append(base.links[0].from_node)
    nodes.extend(n for n in mat.node_tree.nodes if n.type == "TEX_IMAGE")
    for node in nodes:
        if getattr(node, "type", "") == "TEX_IMAGE" and node.image:
            path = bpy.path.abspath(node.image.filepath).replace("\\", "/")
            if path and os.path.isfile(path):
                return path
    return None


class UB_OT_send_to_zbrush(bpy.types.Operator):
    bl_idname = "usd_bridge.send_to_zbrush"
    bl_label = "Send to ZBrush"
    bl_description = "Send selected mesh objects (with vertex colours and textures) to ZBrush"

    _timer = None
    _deadline = 0.0

    def execute(self, context):
        exe = _zbrush_exe()
        if not os.path.isfile(exe):
            self.report({"ERROR"}, "ZBrush.exe not found: %s" % exe)
            return {"CANCELLED"}
        objs = [o for o in context.selected_objects
                if o.type == "MESH" and o.visible_get()]
        if not objs and (context.active_object and
                         context.active_object.type == "MESH" and
                         context.active_object.visible_get()):
            objs = [context.active_object]
        if not objs:
            self.report({"WARNING"}, "Select at least one visible mesh object")
            return {"CANCELLED"}

        send_dir = _cache_dir() + "/bl_send"
        _reset_dir(send_dir)
        prev_selection = list(context.selected_objects)
        prev_active = context.view_layer.objects.active
        send_id = uuid.uuid4().hex[:8]
        files, texfiles, texnames = [], [], []
        try:
            for idx, obj in enumerate(objs):
                bpy.ops.object.select_all(action="DESELECT")
                obj.select_set(True)
                context.view_layer.objects.active = obj
                path = "%s/%d_%s.obj" % (send_dir, idx, _fs_name(obj.name))
                bpy.ops.wm.obj_export(**_kw(bpy.ops.wm.obj_export,
                                            filepath=path,
                                            export_selected_objects=True,
                                            export_colors=True,
                                            export_uv=True,
                                            export_normals=False,
                                            export_materials=False,
                                            apply_modifiers=True,
                                            up_axis="Y",
                                            forward_axis="NEGATIVE_Z"))
                _encode_obj_vertex_colors(path)
                files.append(path)

                tex = _texture_of(obj)
                copied, base = "", ""
                if tex:
                    base = "ubtex_%s_%d" % (send_id, idx)
                    dst = "%s/%s.png" % (send_dir, base)
                    ok = (_flip_image_v(tex, dst) if SEND_FLIP_TEXTURE_V
                          else _copy_plain(tex, dst))
                    if ok:
                        copied = dst
                    else:
                        base = ""
                texfiles.append(copied)
                texnames.append(base)
        finally:
            bpy.ops.object.select_all(action="DESELECT")
            for o in prev_selection:
                try:
                    o.select_set(True)
                except Exception:
                    pass
            context.view_layer.objects.active = prev_active

        rows = []
        for i, mesh in enumerate(files):
            rows.append('[VarSet, files(%d), "%s"]' % (i, mesh))
            rows.append('[VarSet, texfiles(%d), "%s"]' % (i, texfiles[i]))
            rows.append('[VarSet, texnames(%d), "%s"]' % (i, texnames[i]))
        script = (_ZS_SEND
                  .replace("__COUNT__", str(len(files)))
                  .replace("__FILE_LIST__", "\n".join(rows))
                  .replace("__TEX_K__", _ZS_TEX_APPLY.replace("INDEX", "k"))
                  .replace("__TEX_0__", _ZS_TEX_APPLY.replace("INDEX", "0"))
                  .replace("__APPEND__", "1" if SEND_TO_CURRENT_TOOL else "0")
                  .replace("__SIGNAL__", SEND_SIGNAL))
        _clear_signal(SEND_SIGNAL)
        _run_zscript(script, _cache_dir() + "/bl_send.txt")

        self._deadline = time.time() + SIGNAL_TIMEOUT
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.3, window=context.window)
        wm.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type == "ESC":
            return self._finish(context, cancelled=True)
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        if _signal_found(SEND_SIGNAL):
            self.report({"INFO"}, "USD Portal: ZBrush confirmed the import")
            return self._finish(context)
        if time.time() > self._deadline:
            self.report({"WARNING"}, "ZBrush did not confirm in time (it may still be busy)")
            return self._finish(context, cancelled=True)
        return {"PASS_THROUGH"}

    def _finish(self, context, cancelled=False):
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        return {"CANCELLED"} if cancelled else {"FINISHED"}


def _copy_plain(src, dst):
    try:
        copyfile(src, dst)
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# Blender <-> Maya (client side of the same cmd/done protocol)
# --------------------------------------------------------------------------- #
def _maya_dir():
    return _cache_dir() + "/maya"


def _send_app_cmd(xdir, op, payload):
    _ensure_dir(xdir)
    done = xdir + "/done.json"
    try:
        if os.path.isfile(done):
            os.remove(done)
    except OSError:
        pass
    cmd = dict(payload)
    cmd["op"] = op
    cmd["id"] = uuid.uuid4().hex
    _atomic_json(xdir + "/cmd.json", cmd)
    return cmd["id"]


class _AppWaitMixin:
    """Modal wait for an app agent's done.json."""
    _timer = None
    _deadline = 0.0
    _xdir = ""
    _cmd_id = ""

    def _start_wait(self, context, xdir, cmd_id, timeout=120.0):
        self._xdir, self._cmd_id = xdir, cmd_id
        self._deadline = time.time() + timeout
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.3, window=context.window)
        wm.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def _poll_done(self):
        done = self._xdir + "/done.json"
        if not os.path.isfile(done):
            return None
        try:
            with open(done) as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        return data if data.get("id") == self._cmd_id else None

    def _finish(self, context, cancelled=False):
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        return {"CANCELLED"} if cancelled else {"FINISHED"}


class UB_OT_send_to_maya(bpy.types.Operator, _AppWaitMixin):
    bl_idname = "usd_bridge.send_to_maya"
    bl_label = "Send to Maya"
    bl_description = "Export visible selection (or all visible meshes) and import it in Maya"

    def execute(self, context):
        usd = _maya_dir() + "/from_blender.usd"
        try:
            _process_command({"op": "export_usd", "usd": usd})
        except Exception as exc:
            self.report({"ERROR"}, "Export failed: %s" % exc)
            return {"CANCELLED"}
        cmd_id = _send_app_cmd(_maya_dir(), "import_usd", {"usd": usd})
        return self._start_wait(context, _maya_dir(), cmd_id)

    def modal(self, context, event):
        if event.type == "ESC":
            return self._finish(context, cancelled=True)
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        done = self._poll_done()
        if done is not None:
            if done.get("ok"):
                self.report({"INFO"}, "Maya imported %s object(s)" % done.get("objects", "?"))
            else:
                self.report({"WARNING"}, "Maya import failed: %s" % done.get("error"))
            return self._finish(context)
        if time.time() > self._deadline:
            self.report({"WARNING"}, "Maya did not respond (usd_bridge_maya running?)")
            return self._finish(context, cancelled=True)
        return {"PASS_THROUGH"}


class UB_OT_get_from_maya(bpy.types.Operator, _AppWaitMixin):
    bl_idname = "usd_bridge.get_from_maya"
    bl_label = "Get from Maya"
    bl_description = "Ask Maya to export its visible selection and import it here"

    def execute(self, context):
        usd = _cache_dir() + "/blender/from_maya.usd"
        cmd_id = _send_app_cmd(_maya_dir(), "export_usd", {"usd": usd})
        self._usd = usd
        return self._start_wait(context, _maya_dir(), cmd_id)

    def modal(self, context, event):
        if event.type == "ESC":
            return self._finish(context, cancelled=True)
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        done = self._poll_done()
        if done is not None:
            if not done.get("ok"):
                self.report({"WARNING"}, "Maya export failed: %s" % done.get("error"))
                return self._finish(context, cancelled=True)
            try:
                res = _process_command({"op": "import_usd", "usd": self._usd})
                self.report({"INFO"}, "Imported %s object(s) from Maya" % res.get("objects"))
            except Exception as exc:
                self.report({"ERROR"}, "Import failed: %s" % exc)
            return self._finish(context)
        if time.time() > self._deadline:
            self.report({"WARNING"}, "Maya did not respond (usd_bridge_maya running?)")
            return self._finish(context, cancelled=True)
        return {"PASS_THROUGH"}


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
class UB_prefs(bpy.types.AddonPreferences):
    bl_idname = __name__

    cache_dir: bpy.props.StringProperty(
        name="Cache directory", subtype="DIR_PATH", default=DEFAULT_CACHE,
        description="Must match USD_BRIDGE_CACHE of the Houdini package")
    zbrush_exe: bpy.props.StringProperty(
        name="ZBrush executable", subtype="FILE_PATH", default=DEFAULT_ZBRUSH)

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, "cache_dir")
        col.prop(self, "zbrush_exe")


class UB_PT_panel(bpy.types.Panel):
    bl_label = "USD Portal"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "USD Portal"

    def draw(self, context):
        col = self.layout.column(align=True)
        col.operator("usd_bridge.get_from_zbrush", icon="IMPORT")
        col.operator("usd_bridge.send_to_zbrush", icon="EXPORT")
        col.separator()
        col.operator("usd_bridge.get_from_maya", icon="IMPORT")
        col.operator("usd_bridge.send_to_maya", icon="EXPORT")
        col.separator()
        col.label(text="Houdini: use its USD Portal shelf;")
        col.label(text="this add-on answers automatically.")


_classes = (UB_prefs, UB_PT_panel, UB_OT_get_from_zbrush, UB_OT_send_to_zbrush,
            UB_OT_send_to_maya, UB_OT_get_from_maya)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    _ensure_dir(_blender_exchange_dir())
    if not bpy.app.timers.is_registered(_watcher_tick):
        bpy.app.timers.register(_watcher_tick, first_interval=1.0, persistent=True)


def unregister():
    if bpy.app.timers.is_registered(_watcher_tick):
        bpy.app.timers.unregister(_watcher_tick)
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()

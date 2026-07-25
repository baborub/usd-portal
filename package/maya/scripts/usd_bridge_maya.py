"""
usd_bridge_maya - Maya 2026 side of the usd_bridge (Houdini / ZBrush / Blender / Maya).

Jobs:

1. Serve other apps. A QTimer watches <cache>/maya/cmd.json; Houdini's
   "Send to Maya" / "Get from Maya" shelf tools (and Blender's panel buttons) write
   commands; this agent imports/exports USD via mayaUsd and answers with done.json.

2. Drive ZBrush directly ("Get from ZBrush" / "Send to ZBrush" in the USD Portal
   menu): the same ZScript machinery as the Houdini/Blender sides, over OBJ.
   Maya's OBJ io has no vertex-colour support, so polypaint is carried manually:
   #MRGB blocks are parsed on import (-> a 'polypaint' colour set, sRGB->linear)
   and injected as per-vertex 'v x y z r g b' colours on export (linear->sRGB).

3. Talk to Blender directly (menu items) using the same cmd/done protocol against
   <cache>/blender/ - Blender's add-on watcher serves them.

Install (deploy.ps1 does this): copy to Documents/maya/2026/scripts/ and add to
userSetup.py:  import usd_bridge_maya; usd_bridge_maya.start()

Export rule: only VISIBLE meshes travel (the visible selection when there is one).
A colour set literally named 'displayColor' would be exported EMPTY by mayaUsd
(reserved name) - the agent temp-renames it to 'Cd' around the export.
"""

import glob
import json
import os
import re
import subprocess
import time
import uuid
from shutil import copyfile, rmtree

import maya.cmds as cmds
import maya.utils
import maya.api.OpenMaya as om2

try:
    from PySide6 import QtCore, QtGui, QtWidgets
except ImportError:                      # pragma: no cover
    from PySide2 import QtCore, QtGui, QtWidgets

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
CACHE_DIR = os.getenv("USD_BRIDGE_CACHE", "C:/usd_bridge_cache").replace("\\", "/")
ZBRUSH_EXE = os.getenv("ZBRUSH_EXEC_PATH", "C:/Program Files/Maxon ZBrush 2026/ZBrush.exe")

GET_SIGNAL = "ub_myget_done"
SEND_SIGNAL = "ub_mysend_done"
SIGNAL_TIMEOUT = 180.0
APP_TIMEOUT = 120.0

GET_FLIP_TEXTURE_V = True        # ZBrush OBJ UVs vs its Texture Map (see other agents)
SEND_FLIP_TEXTURE_V = True
SEND_TO_CURRENT_TOOL = False

# The bridge convention is METERS (Houdini/USD/Blender); Maya works in centimeters.
# The USD legs are converted by mayaUsd automatically, but the ZBrush OBJ leg carries
# raw numbers - scale it ourselves so sizes match physically across all apps.
ZB_UNIT_SCALE = 100.0            # Maya cm per bridge meter

_timer = None
_menu_name = "usdBridgeMenu"


def _ensure_dir(p):
    if not os.path.isdir(p):
        os.makedirs(p)


def _reset_dir(p):
    if os.path.isdir(p):
        rmtree(p, ignore_errors=True)
    os.makedirs(p)


def _fs_name(name):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name).strip("_") or "mesh"


def _atomic_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def _maya_dir():
    return CACHE_DIR + "/maya"


# --------------------------------------------------------------------------- #
# Visibility
# --------------------------------------------------------------------------- #
def _shape_visible(shape):
    parts = shape.split("|")[1:]
    path = ""
    for p in parts:
        path += "|" + p
        try:
            if not cmds.getAttr(path + ".visibility"):
                return False
        except Exception:
            pass
    return True


def _visible_mesh_shapes():
    shapes = cmds.ls(type="mesh", noIntermediate=True, long=True) or []
    return [s for s in shapes if _shape_visible(s)]


def _export_target_transforms():
    """Visible selection when there is one, else all visible meshes."""
    visible = _visible_mesh_shapes()
    sel = set(cmds.ls(sl=True, dag=True, type="mesh", noIntermediate=True, long=True) or [])
    targets = [s for s in visible if s in sel] or visible
    return sorted(set(cmds.listRelatives(s, parent=True, fullPath=True)[0] for s in targets))


# --------------------------------------------------------------------------- #
# USD import / export (mayaUsd)
# --------------------------------------------------------------------------- #
def _load_plugins():
    for plug in ("mayaUsdPlugin", "objExport"):
        if not cmds.pluginInfo(plug, q=True, loaded=True):
            cmds.loadPlugin(plug)


def _enable_vertex_color_display(shapes):
    """Make imported vertex colours actually visible in the shaded viewport: pick a
    colour set (displayColor preferred), turn on displayColors AND route the colours
    into the material's ambient+diffuse channel (the Color Set Editor's
    'Color in Shaded Display On') - with a material bound, displayColors alone
    changes nothing."""
    for s in shapes:
        try:
            sets = cmds.polyColorSet(s, q=True, allColorSets=True) or []
            if not sets:
                continue
            current = "displayColor" if "displayColor" in sets else sets[0]
            cmds.polyColorSet(s, currentColorSet=True, colorSet=current)
            cmds.setAttr(s + ".displayColors", 1)
            cmds.polyOptions(s, colorShadedDisplay=True,
                             colorMaterialChannel="ambientDiffuse")
        except Exception:
            pass


def _fix_vertexcolor_materials(shapes):
    """VP2 draws usdPreviewSurface with its own shader that IGNORES per-vertex
    colour display (colorShadedDisplay works only with native materials). Meshes
    that carry a colour set but no texture get the default shading group instead -
    the exact setup the ZBrush leg uses, which displays colours correctly."""
    for s in shapes:
        try:
            if not (cmds.polyColorSet(s, q=True, allColorSets=True) or []):
                continue
            if _texture_of_shape(s):
                continue                     # textured mesh: keep its material
            cmds.sets(s, e=True, forceElement="initialShadingGroup")
        except Exception:
            pass


def import_usd(usd_path):
    _load_plugins()
    before = set(cmds.ls(type="mesh", long=True) or [])
    cmds.mayaUSDImport(file=usd_path.replace("\\", "/"), readAnimData=False)
    new = sorted(set(cmds.ls(type="mesh", long=True) or []) - before)
    _enable_vertex_color_display(new)
    _fix_vertexcolor_materials(new)
    return {"objects": len(new)}


def _normalize_exported_usd(usd_path):
    """Post-fix a mayaUSDExport stage so other apps read it cleanly:
    - promote the first colour primvar (colour sets export under their own name,
      e.g. primvars:polypaint) to the USD-conventional displayColor;
    - strip empty material bindings (Maya's default initialShadingGroup exports as
      a Material prim with NO surface shader - Blender turns that into a black
      'No output node' material that overrides everything)."""
    try:
        from pxr import Usd, UsdGeom, UsdShade, Sdf, Gf, Vt
    except ImportError:
        return 0
    layer = Sdf.Layer.Find(usd_path)
    if layer:
        layer.Reload(force=True)
    stage = Usd.Stage.Open(usd_path)
    dirty = 0
    for prim in stage.Traverse():
        if prim.GetTypeName() != "Mesh":
            continue
        try:
            api = UsdGeom.PrimvarsAPI(prim)
            dc = api.GetPrimvar("displayColor")
            flattened = dc.ComputeFlattened() if dc and dc.HasValue() else None
            if not flattened or len(flattened) == 0:
                candidates = []
                for pv in api.GetPrimvars():
                    name = pv.GetPrimvarName()
                    if name in ("displayColor", "displayOpacity") or not pv.HasValue():
                        continue
                    type_name = str(pv.GetTypeName())
                    if type_name.startswith("color3f") or type_name.startswith("color4f"):
                        candidates.append(pv)
                pick = next((p for p in candidates
                             if p.GetPrimvarName().lower() in ("polypaint", "cd", "color")),
                            candidates[0] if candidates else None)
                values = pick.ComputeFlattened() if pick else None
                if values:
                    # Maya colour sets are display-referred (sRGB) and mayaUsd
                    # exports them raw; USD displayColor must be linear -> decode.
                    out = api.CreatePrimvar("displayColor",
                                            Sdf.ValueTypeNames.Color3fArray,
                                            pick.GetInterpolation())
                    out.Set(Vt.Vec3fArray([Gf.Vec3f(_srgb_to_linear(v[0]),
                                                    _srgb_to_linear(v[1]),
                                                    _srgb_to_linear(v[2]))
                                           for v in values]))
                    dirty += 1

            binding = UsdShade.MaterialBindingAPI(prim)
            material = binding.ComputeBoundMaterial()[0]
            if material and material.GetPrim().IsValid():
                surface = material.ComputeSurfaceSource()[0]
                if not surface:                    # empty material (initialShadingGroup)
                    binding.UnbindAllBindings()
                    dirty += 1
        except Exception:
            continue
    if dirty:
        stage.GetRootLayer().Save()
    return dirty


def export_usd(usd_path):
    _load_plugins()
    _ensure_dir(os.path.dirname(usd_path))
    transforms = _export_target_transforms()
    if not transforms:
        raise RuntimeError("no visible mesh objects to export")

    # mayaUsd exports a colour set literally named 'displayColor' as EMPTY
    # (reserved for the shader-derived value) - rename around the export.
    renamed = []
    for tr in transforms:
        for shape in cmds.listRelatives(tr, shapes=True, fullPath=True) or []:
            sets = cmds.polyColorSet(shape, q=True, allColorSets=True) or []
            if "displayColor" in sets and "Cd" not in sets:
                cmds.polyColorSet(shape, rename=True,
                                  colorSet="displayColor", newColorSet="Cd")
                renamed.append(shape)

    prev_sel = cmds.ls(sl=True, long=True) or []
    try:
        cmds.select(transforms, r=True)
        cmds.mayaUSDExport(file=usd_path.replace("\\", "/"), selection=True,
                           exportColorSets=True, exportUVs=True,
                           shadingMode="useRegistry",
                           convertMaterialsTo=["UsdPreviewSurface"],
                           defaultUSDFormat="usdc",
                           mergeTransformAndShape=True, stripNamespaces=True)
    finally:
        for shape in renamed:
            try:
                cmds.polyColorSet(shape, rename=True, colorSet="Cd",
                                  newColorSet="displayColor")
            except Exception:
                pass
        try:
            cmds.select(prev_sel, r=True) if prev_sel else cmds.select(clear=True)
        except Exception:
            pass
    _normalize_exported_usd(usd_path)
    return {"objects": len(transforms)}


# --------------------------------------------------------------------------- #
# Command server (cmd.json -> done.json)
# --------------------------------------------------------------------------- #
def _process_command(cmd):
    op = cmd.get("op")
    if op == "import_usd":
        return import_usd(cmd["usd"])
    if op == "export_usd":
        return export_usd(cmd["usd"])
    raise ValueError("unknown op: %r" % op)


def _tick():
    xdir = _maya_dir()
    cmd_path = xdir + "/cmd.json"
    if not os.path.isfile(cmd_path):
        return
    try:
        with open(cmd_path) as fh:
            cmd = json.load(fh)
    except (OSError, ValueError):
        return                            # half-written; next tick
    try:
        os.remove(cmd_path)
    except OSError:
        pass
    try:
        result = _process_command(cmd)
        result["ok"] = 1
    except Exception as exc:
        result = {"ok": 0, "error": str(exc)}
    result["id"] = cmd.get("id", "")
    try:
        _atomic_json(xdir + "/done.json", result)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Client side (Maya -> Blender over the same protocol)
# --------------------------------------------------------------------------- #
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


def _wait_app_done(xdir, cmd_id, timeout=APP_TIMEOUT):
    done = xdir + "/done.json"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.isfile(done):
            try:
                with open(done) as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                data = None
            if data and data.get("id") == cmd_id:
                return data
        QtWidgets.QApplication.processEvents()
        time.sleep(0.15)
    return None


def send_to_blender():
    usd = CACHE_DIR + "/blender/from_maya.usd"
    try:
        export_usd(usd)
    except Exception as exc:
        cmds.warning("USD Portal: %s" % exc)
        return
    cmd_id = _send_app_cmd(CACHE_DIR + "/blender", "import_usd", {"usd": usd})
    done = _wait_app_done(CACHE_DIR + "/blender", cmd_id)
    if done is None:
        cmds.warning("USD Portal: Blender did not respond (add-on enabled?)")
    elif not done.get("ok"):
        cmds.warning("USD Portal: Blender import failed: %s" % done.get("error"))
    else:
        print("USD Portal: sent to Blender (%s object(s))" % done.get("objects", "?"))


def get_from_blender():
    usd = _maya_dir() + "/from_blender.usd"
    cmd_id = _send_app_cmd(CACHE_DIR + "/blender", "export_usd", {"usd": usd})
    done = _wait_app_done(CACHE_DIR + "/blender", cmd_id)
    if done is None:
        cmds.warning("USD Portal: Blender did not respond (add-on enabled?)")
        return
    if not done.get("ok"):
        cmds.warning("USD Portal: Blender export failed: %s" % done.get("error"))
        return
    result = import_usd(usd)
    print("USD Portal: imported %d mesh(es) from Blender" % result["objects"])


# --------------------------------------------------------------------------- #
# ZBrush signalling + templates (same machinery as the other agents)
# --------------------------------------------------------------------------- #
def _signal_paths(name):
    filename = name + ".zvr"
    paths = [os.path.join(CACHE_DIR, filename)]
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


def _wait_signal(name, timeout=SIGNAL_TIMEOUT):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _signal_found(name):
            return True
        QtWidgets.QApplication.processEvents()
        time.sleep(0.15)
    return False


def _run_zscript(text, script_path):
    _ensure_dir(os.path.dirname(script_path))
    with open(script_path, "w") as fh:
        fh.write(text)
    subprocess.Popen([ZBRUSH_EXE, script_path])


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
# Colour helpers (sRGB <-> linear; USD/Maya colour sets are linear)
# --------------------------------------------------------------------------- #
def _srgb_to_linear(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(c):
    c = max(0.0, min(1.0, c))
    return c * 12.92 if c <= 0.0031308 else 1.055 * (c ** (1.0 / 2.4)) - 0.055


def _flip_image_v(src, dst):
    try:
        img = QtGui.QImage(src)
        if img.isNull():
            raise ValueError("unreadable image")
        try:
            flipped = img.mirrored(False, True)
        except AttributeError:            # Qt 6.9+: flipped()
            flipped = img.flipped(QtCore.Qt.Orientation.Vertical)
        if not flipped.save(dst):
            raise ValueError("save failed")
        return True
    except Exception:
        try:
            copyfile(src, dst)
            return True
        except OSError:
            return False


# --------------------------------------------------------------------------- #
# ZBrush -> Maya
# --------------------------------------------------------------------------- #
def _parse_mrgb(obj_path):
    """ZBrush polypaint from '#MRGB' blocks (MMRRGGBB per vertex), kept RAW:
    Maya colour sets are display-referred (mayaUsd itself imports USD displayColor
    with an sRGB encode), so ZBrush's sRGB bytes go in unchanged."""
    colors = []
    try:
        with open(obj_path) as fh:
            for line in fh:
                if line.startswith("#MRGB "):
                    h = line.strip().split(" ", 1)[1]
                    for i in range(0, len(h) - 7, 8):
                        r = int(h[i + 2:i + 4], 16) / 255.0
                        g = int(h[i + 4:i + 6], 16) / 255.0
                        b = int(h[i + 6:i + 8], 16) / 255.0
                        colors.append((r, g, b))
    except OSError:
        pass
    return colors


def _apply_vertex_colors(shape, colors):
    sel = om2.MSelectionList()
    sel.add(shape)
    fn = om2.MFnMesh(sel.getDagPath(0))
    n = min(fn.numVertices, len(colors))
    if n == 0:
        return False
    arr = om2.MColorArray()
    for i in range(n):
        r, g, b = colors[i]
        arr.append(om2.MColor((r, g, b, 1.0)))
    try:
        fn.createColorSet("polypaint", False)
    except Exception:
        pass
    fn.setCurrentColorSetName("polypaint")
    fn.setVertexColors(arr, om2.MIntArray(range(n)))
    cmds.setAttr(shape + ".displayColors", 1)
    return True


def _assign_texture_material(shape, name, image_path):
    shader = cmds.shadingNode("standardSurface", asShader=True, name="UB_" + name)
    sg = cmds.sets(renderable=True, noSurfaceShader=True, empty=True,
                   name="UB_%sSG" % name)
    cmds.connectAttr(shader + ".outColor", sg + ".surfaceShader", force=True)
    fnode = cmds.shadingNode("file", asTexture=True, isColorManaged=True,
                             name="UB_%s_tex" % name)
    cmds.setAttr(fnode + ".fileTextureName", image_path, type="string")
    cmds.connectAttr(fnode + ".outColor", shader + ".baseColor", force=True)
    cmds.sets(shape, e=True, forceElement=sg)


def get_from_zbrush():
    if not os.path.isfile(ZBRUSH_EXE):
        cmds.warning("USD Portal: ZBrush.exe not found: %s" % ZBRUSH_EXE)
        return
    obj_dir = CACHE_DIR + "/my_get"
    tex_dir = CACHE_DIR + "/my_get_tex"
    _reset_dir(obj_dir)
    _reset_dir(tex_dir)
    script = (_ZS_GET
              .replace("__EXPORT_DIR__", obj_dir + "/")
              .replace("__TEX_DIR__", tex_dir + "/")
              .replace("__SIGNAL__", GET_SIGNAL))
    _clear_signal(GET_SIGNAL)
    _run_zscript(script, CACHE_DIR + "/my_get.txt")

    if not _wait_signal(GET_SIGNAL):
        cmds.warning("USD Portal: ZBrush did not answer in time")
        return

    # ZBrush drops an .mtl next to each OBJ (map_Kd -> a .bmp that usually does not
    # even exist thanks to its sandbox). Maya's OBJ importer would auto-build a
    # rogue material from it that hijacks the display ("polypaint became a broken
    # texture"). Kill the .mtl files so OUR polypaint/texture logic stays in charge.
    for mtl in glob.glob(obj_dir + "/*.mtl"):
        try:
            os.remove(mtl)
        except OSError:
            pass

    files = sorted(glob.glob(obj_dir + "/*.obj") + glob.glob(obj_dir + "/*.OBJ"))
    files = sorted(set(files),
                   key=lambda p: (int(re.match(r"(\d+)_", os.path.basename(p)).group(1))
                                  if re.match(r"(\d+)_", os.path.basename(p)) else 0,
                                  os.path.basename(p).lower()))
    imported = 0
    for path in files:
        stem = os.path.splitext(os.path.basename(path))[0]
        nice = re.sub(r"^\d+_", "", stem) or "subtool"
        before = set(cmds.ls(type="mesh", long=True) or [])
        try:
            cmds.file(path, i=True, type="OBJ", ignoreVersion=True, options="mo=0")
        except Exception:
            continue
        new = sorted(set(cmds.ls(type="mesh", long=True) or []) - before)
        if not new:
            continue
        shape = new[0]
        transform = cmds.listRelatives(shape, parent=True, fullPath=True)[0]
        transform = cmds.rename(transform, _fs_name(nice))
        shape = (cmds.listRelatives(transform, shapes=True, fullPath=True) or [shape])[0]

        # bridge meters -> Maya centimeters, baked into the mesh
        if ZB_UNIT_SCALE != 1.0:
            cmds.scale(ZB_UNIT_SCALE, ZB_UNIT_SCALE, ZB_UNIT_SCALE, transform,
                       absolute=True)
            cmds.makeIdentity(transform, apply=True, scale=True)

        colors = _parse_mrgb(path)
        if colors:
            _apply_vertex_colors(shape, colors)

        candidates = [p for p in glob.glob("%s/%s.*" % (tex_dir, stem))
                      if not p.endswith("_flip.png")]
        candidates.sort(key=lambda p: 0 if p.lower().endswith(".png") else 1)
        if candidates:
            tex = candidates[0]
            use = tex
            if GET_FLIP_TEXTURE_V:
                flipped = "%s/%s_flip.png" % (tex_dir, stem)
                if _flip_image_v(tex, flipped):
                    use = flipped
            _assign_texture_material(shape, _fs_name(nice), use)
        imported += 1
    print("USD Portal: imported %d subtool(s) from ZBrush" % imported)


# --------------------------------------------------------------------------- #
# Maya -> ZBrush
# --------------------------------------------------------------------------- #
def _postprocess_obj_for_zbrush(obj_path, shape, scale):
    """Rewrite the OBJ's v-lines: scale Maya cm -> bridge meters, and append
    per-vertex colours (ZBrush reads the 'v x y z r g b' extension). Maya colour
    sets are display-referred (sRGB) - exactly what ZBrush expects - so values go
    out unchanged. Vertex order in Maya's OBJ = vertex ids."""
    colors = None
    try:
        sel = om2.MSelectionList()
        sel.add(shape)
        fn = om2.MFnMesh(sel.getDagPath(0))
        cset = fn.currentColorSetName()
        if not cset:
            sets = cmds.polyColorSet(shape, q=True, allColorSets=True) or []
            cset = sets[0] if sets else None
        if cset:
            colors = fn.getVertexColors(cset, om2.MColor((1.0, 1.0, 1.0, 1.0)))
    except Exception:
        colors = None
    try:
        with open(obj_path) as fh:
            lines = fh.readlines()
    except OSError:
        return False
    vi = 0
    for i, line in enumerate(lines):
        if not line.startswith("v "):
            continue
        parts = line.split()
        try:
            xyz = ["%.8f" % (float(x) * scale) for x in parts[1:4]]
        except (ValueError, IndexError):
            continue
        rgb = []
        if colors is not None and vi < len(colors):
            c = colors[vi]
            rgb = ["%.6f" % max(0.0, min(1.0, x)) for x in (c.r, c.g, c.b)]
        lines[i] = " ".join(["v"] + xyz + rgb) + "\n"
        vi += 1
    with open(obj_path, "w") as fh:
        fh.writelines(lines)
    return True


def _texture_of_shape(shape):
    for sg in set(cmds.listConnections(shape, type="shadingEngine") or []):
        for surf in cmds.listConnections(sg + ".surfaceShader") or []:
            for node in cmds.listHistory(surf) or []:
                if cmds.nodeType(node) == "file":
                    path = (cmds.getAttr(node + ".fileTextureName") or "").replace("\\", "/")
                    if path and os.path.isfile(path):
                        return path
    return None


def send_to_zbrush():
    if not os.path.isfile(ZBRUSH_EXE):
        cmds.warning("USD Portal: ZBrush.exe not found: %s" % ZBRUSH_EXE)
        return
    _load_plugins()
    transforms = _export_target_transforms()
    if not transforms:
        cmds.warning("USD Portal: no visible mesh objects to send")
        return

    send_dir = CACHE_DIR + "/my_send"
    _reset_dir(send_dir)
    prev_sel = cmds.ls(sl=True, long=True) or []
    send_id = uuid.uuid4().hex[:8]
    files, texfiles, texnames = [], [], []
    try:
        for idx, tr in enumerate(transforms):
            shape = (cmds.listRelatives(tr, shapes=True, fullPath=True) or [None])[0]
            if shape is None:
                continue
            path = "%s/%d_%s.obj" % (send_dir, idx, _fs_name(tr.split("|")[-1]))
            cmds.select(tr, r=True)
            cmds.file(path, force=True, exportSelected=True, type="OBJexport",
                      options="groups=0;ptgroups=0;materials=0;smoothing=0;normals=0")
            _postprocess_obj_for_zbrush(path, shape, 1.0 / ZB_UNIT_SCALE)
            files.append(path)

            tex = _texture_of_shape(shape)
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
        try:
            cmds.select(prev_sel, r=True) if prev_sel else cmds.select(clear=True)
        except Exception:
            pass

    if not files:
        cmds.warning("USD Portal: nothing exported")
        return

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
    _run_zscript(script, CACHE_DIR + "/my_send.txt")
    if _wait_signal(SEND_SIGNAL):
        print("USD Portal: ZBrush confirmed the import (%d subtool(s))" % len(files))
    else:
        cmds.warning("USD Portal: ZBrush did not confirm in time")


def _copy_plain(src, dst):
    try:
        copyfile(src, dst)
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# UI + lifecycle
# --------------------------------------------------------------------------- #
def use_untonemapped_view():
    """Maya 2026 views everything through ACES tone mapping by default, which lifts
    shadows / desaturates - vertex colours and textures look washed-out compared to
    ZBrush/Houdini. Switch the view transform to the un-tone-mapped sRGB view."""
    for name in ("Un-tone-mapped (sRGB)", "Un-tonemapped (sRGB)", "Raw (sRGB)",
                 "sRGB gamma", "Raw"):
        try:
            cmds.colorManagementPrefs(e=True, viewTransformName=name)
            print("USD Portal: view transform set to '%s'" % name)
            return
        except Exception:
            continue
    cmds.warning("USD Portal: could not find an un-tone-mapped view transform")


def _build_menu():
    if cmds.about(batch=True):
        return
    if cmds.menu(_menu_name, exists=True):
        cmds.deleteUI(_menu_name)
    menu = cmds.menu(_menu_name, label="USD Portal", parent="MayaWindow", tearOff=True)
    cmds.menuItem(label="Get from ZBrush", parent=menu,
                  command=lambda *_: get_from_zbrush())
    cmds.menuItem(label="Send to ZBrush", parent=menu,
                  command=lambda *_: send_to_zbrush())
    cmds.menuItem(divider=True, parent=menu)
    cmds.menuItem(label="Get from Blender", parent=menu,
                  command=lambda *_: get_from_blender())
    cmds.menuItem(label="Send to Blender", parent=menu,
                  command=lambda *_: send_to_blender())
    cmds.menuItem(divider=True, parent=menu)
    cmds.menuItem(label="Viewport: Un-tone-mapped Colors", parent=menu,
                  command=lambda *_: use_untonemapped_view())
    cmds.menuItem(label="(Houdini: use its USD Portal shelf)", parent=menu, enable=False)


def start():
    """Idempotent: watcher timer + menu. Call from userSetup.py."""
    global _timer
    _ensure_dir(_maya_dir())
    if _timer is None:
        _timer = QtCore.QTimer()
        _timer.setInterval(500)
        _timer.timeout.connect(_tick)
        _timer.start()
    maya.utils.executeDeferred(_build_menu)


def stop():
    global _timer
    if _timer is not None:
        _timer.stop()
        _timer = None

"""
usd_bridge - ZBrush 2026 <-> Houdini 22 bridge over USD.

v1 direction: ZBrush -> Houdini ("Get from ZBrush").

Pipeline
--------
1. ZBrush exports every VISIBLE subtool to GoZ, one file per subtool, driven by a
   generated ZScript (FileNameSetNext + Tool:Export). GoZ is used as the transport
   because it reliably carries UVs + polypaint by an explicit path; the ZBrush USD
   Format plugin cannot be driven from ZScript with a target path (it opens its own
   file dialog and ignores FileNameSetNext), so we author the USD on the Houdini side.
2. Houdini loads each GoZ mesh and authors ONE USD stage: a Mesh prim per subtool
   under /root, carrying primvars:st (UV) and primvars:displayColor (polypaint).
   That .usd is the interchange artifact.
3. The stage is imported back into SOPs via usdimport -> unpackusd (polygons),
   which returns clean polygons with `uv` and `Cd` (st->uv is auto-translated).

Environment (set by the Houdini package, deploy/usd_bridge.json)
    ZBRUSH_EXEC_PATH   full path to ZBrush.exe
    USD_BRIDGE_CACHE   scratch dir for the GoZ / usd / zscript exchange

Houdini 22.0 runs Python 3.13 + PySide6, hence the python3.13libs location.
"""

import hou
import os
import re
import glob
import json
import time
import uuid
from shutil import rmtree, copyfile

try:
    from PySide6.QtCore import QProcess
    from PySide6 import QtWidgets as _qtw
except ImportError:                       # pragma: no cover - older Houdini
    from PySide2.QtCore import QProcess
    from PySide2 import QtWidgets as _qtw

import numpy as np
from pxr import Usd, UsdGeom, UsdShade, Sdf, Vt, Gf


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def _env(name, default):
    value = os.getenv(name)
    return value if value else default


CACHE_DIR = _env("USD_BRIDGE_CACHE", "C:/usd_bridge_cache").replace("\\", "/")
ZBRUSH_EXE = _env("ZBRUSH_EXEC_PATH", "C:/Program Files/Maxon ZBrush 2026/ZBrush.exe")

# VarSave writes "<name>.zvr". Bare-named writes are redirected by ZBrush 2026 into
# its own temp sandbox (see _signal_paths), so we scan both the cache and that temp.
GET_SIGNAL = "usdbridge_get_done"
SIGNAL_TIMEOUT = 180.0

# Where "Get from ZBrush" drops the result. "auto" = LOP if you're working in a LOP
# network (/stage or a lopnet), else SOPs. Force with "sop" / "lop". The LOP path just
# sublayers the authored .usd, so its UsdPreviewSurface materials come in natively.
GET_TARGET = "auto"

# Blender exchange (Houdini <-> Blender, pure USD + a tiny JSON command protocol).
# Blender runs the usd_bridge_blender add-on whose background timer watches
# <cache>/blender/cmd.json, executes the op (import_usd / export_usd), and answers
# via done.json. Both sides write files atomically (tmp + os.replace).
BLENDER_TIMEOUT = 120.0

# Texture map transfer. Each subtool's Tool>Texture Map is exported next to its GoZ,
# authored into the USD as a UsdPreviewSurface and rebuilt as a Principled Shader in
# SOPs. FLIP_TEXTURE_V flips the `st` V at authoring time (affects both the USD
# material and the SOP texture). Verified on a real subtool: ZBrush's GoZ UVs already
# match Houdini/USD, so NO flip is needed - leave False. Flip only if a future
# map/UV setup comes in upside-down.
EXPORT_TEXTURES = True
FLIP_TEXTURE_V = False

# Send (Houdini -> ZBrush). Transport is OBJ because ZBrush's Tool:Import reads it
# reliably (it carries UVs; polypaint/Cd does not travel this way). Geometry is
# baked to world space and split into subtools.
SEND_SIGNAL = "usdbridge_send_done"
SEND_FORMAT = "obj"
SEND_TO_CURRENT_TOOL = False   # False = import as a NEW tool (robust: SimpleBrush+Import
                               # establishes Edit mode, so SubTool:Duplicate is enabled).
                               # True = append to the active tool - only works when that
                               # tool is active and in Edit mode (Duplicate is disabled,
                               # i.e. "not found", otherwise); the script tries to enter it.
SEND_SPLIT = "auto"            # "auto" = split by name/path attribute, else one subtool;
                               # "single" = always one subtool
SEND_FLIP_NORMALS = True       # make winding ZBrush-compatible before OBJ export.
                               # Houdini-native geometry needs a reverse (opposite
                               # winding conventions); geometry unpacked from USD
                               # (usdimport/unpackusd, tagged usdconfigreversepolygons)
                               # is ALREADY opposite-wound and is sent as-is - flipping
                               # it too would invert normals in ZBrush.
SEND_TEXTURES = True           # also send each subtool's diffuse texture (from its
                               # Principled Shader) and assign it to Tool>Texture Map.
                               # Each texture is copied to a unique name so ZBrush names
                               # it predictably (no "(1)" collision suffix) for the popup.
SEND_FLIP_TEXTURE_V = True     # flip the texture vertically on the way in: ZBrush's
                               # Texture Map expects the opposite V orientation, so an
                               # unflipped map arrives upside-down
SEND_POLYPAINT = True          # send vertex colour (Cd) as OBJ vertex colours
SEND_CD_COLORSPACE = "srgb"    # ZBrush decodes OBJ vertex colours (sRGB->linear), so a
                               # raw Cd lands over-saturated. "srgb" pre-encodes Cd
                               # (linear->sRGB) to cancel it; "linear" does the opposite;
                               # "none" sends Cd unchanged


# --------------------------------------------------------------------------- #
# ZScript: export every visible subtool to GoZ
# --------------------------------------------------------------------------- #
# Placeholders __EXPORT_DIR__ (trailing slash) and __SIGNAL__ are filled in below.
# Visibility bitfield from SubToolGetStatus: 0x1 = subtool eye on, 0x2 = folder open.
_ZS_EXPORT_GOZ = r"""[If, 1,
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
                [StrMerge, [StrMerge, [StrMerge, i, "_"], nm], ".GoZ"]]]
            [IPress, Tool:Export]
            __TEXTURE_BLOCK__
        ]
        [VarInc, i]
    ]
    [SubToolSelect, restoreIndex]
]
[VarSet, done, 0]
[VarSave, done, __SIGNAL__]
]"""

# Injected per subtool when EXPORT_TEXTURES is on. Guarded by IsEnabled so subtools
# without a Texture Map are skipped. Clone Txtr copies the tool texture into the
# Texture palette (required before it can be exported); Texture:Export honours the
# FileNameSetNext path. The V-flip is done Houdini-side on `st` (see _author_mesh),
# not here - the Texture palette has no scriptable FlipV item.
_ZS_TEXTURE_BLOCK = r"""[If, [IsEnabled, Tool:Texture Map:Clone Txtr],
                [IPress, Tool:Texture Map:Clone Txtr]
                [FileNameSetNext, [StrMerge, "__TEX_DIR__",
                    [StrMerge, [StrMerge, [StrMerge, i, "_"], nm], ".png"]]]
                [IPress, Texture:Export]
            ]"""

# Import N meshes into ZBrush as subtools. __APPEND__ 1 + a tool with subtools -> each
# mesh is Duplicated + Imported onto the current tool; otherwise the first mesh starts a
# fresh tool (via the PolyMesh3D star, NOT SimpleBrush - switching to a 2.5D brush drops
# an edited 3D tool onto the canvas) and the rest are appended.
# SubTool:Duplicate needs Edit mode; the canvas is only touched when Edit is off, so a
# tool already being sculpted is left exactly as it was.
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

# Injected after a subtool's mesh is imported (INDEX is "k" inside a loop, "0" for the
# first new-tool import). Imports the subtool's texture and assigns it to the subtool's
# Texture Map by picking it from the popup by name (ZBrush names an imported texture
# after its file; unique file names keep that predictable). No-op when the name is empty.
_ZS_TEX_APPLY = """[If, ([StrLength, [Var, texnames(INDEX)]] > 0),
                [FileNameSetNext, [Var, texfiles(INDEX)]]
                [IPress, Texture:Import]
                [IPress, Tool:Texture Map:TextureMap]
                [IPress, [StrMerge, "PopUp:", [Var, texnames(INDEX)]]]
            ]"""


# --------------------------------------------------------------------------- #
# USD authoring  (module level so it can be unit-tested head-less with hython)
# --------------------------------------------------------------------------- #
def _prim_name(name, used):
    """Sanitise a subtool name into a unique, valid USD prim name."""
    clean = re.sub(r"[^A-Za-z0-9_]", "_", name or "").strip("_")
    if not clean:
        clean = "subtool"
    if clean[0].isdigit():
        clean = "_" + clean
    base, n = clean, 1
    while clean in used:
        n += 1
        clean = "%s_%d" % (base, n)
    used.add(clean)
    return clean


def _topology(geo):
    """Return (faceVertexCounts, faceVertexIndices) in Houdini linear vertex order.

    This is the one unavoidable per-vertex pass. It is fine for the mesh sizes a
    sculptor usually bridges; for very heavy meshes a SOP->USD-ROP backend would be
    faster (see README, 'Known limitations').
    """
    counts, indices = [], []
    for prim in geo.iterPrims():
        verts = prim.vertices()
        counts.append(len(verts))
        for v in verts:
            indices.append(v.point().number())
    return counts, indices


def _uv_source(geo):
    """UVs live on vertices in Houdini/GoZ; fall back to points."""
    a = geo.findVertexAttrib("uv")
    if a is not None:
        return "vertex", a.size()
    a = geo.findPointAttrib("uv")
    if a is not None:
        return "point", a.size()
    return None, 0


def _cd_source(geo):
    """Polypaint arrives as point Cd from GoZ; fall back to vertices."""
    if geo.findPointAttrib("Cd") is not None:
        return "point"
    if geo.findVertexAttrib("Cd") is not None:
        return "vertex"
    return None


def _author_mesh(stage, prim_path, geo):
    mesh = UsdGeom.Mesh.Define(stage, prim_path)

    # Points
    pts = np.asarray(geo.pointFloatAttribValues("P"), dtype=np.float32).reshape(-1, 3)
    mesh.CreatePointsAttr(Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(pts)))

    # Topology
    counts, indices = _topology(geo)
    mesh.CreateFaceVertexCountsAttr(Vt.IntArray(counts))
    mesh.CreateFaceVertexIndicesAttr(Vt.IntArray(indices))
    mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)      # keep as polygons
    mesh.CreateOrientationAttr(UsdGeom.Tokens.rightHanded)

    if len(pts):
        lo, hi = pts.min(axis=0), pts.max(axis=0)
        mesh.CreateExtentAttr([Gf.Vec3f(*[float(x) for x in lo]),
                               Gf.Vec3f(*[float(x) for x in hi])])

    api = UsdGeom.PrimvarsAPI(mesh)

    # UV -> primvars:st
    uv_class, uv_size = _uv_source(geo)
    if uv_class:
        raw = (geo.vertexFloatAttribValues("uv") if uv_class == "vertex"
               else geo.pointFloatAttribValues("uv"))
        uvv = np.array(raw, dtype=np.float32).reshape(-1, uv_size)[:, :2]
        if FLIP_TEXTURE_V:
            uvv = uvv.copy()
            uvv[:, 1] = 1.0 - uvv[:, 1]        # ZBrush -> Houdini/USD V origin
        interp = UsdGeom.Tokens.faceVarying if uv_class == "vertex" else UsdGeom.Tokens.vertex
        st = api.CreatePrimvar("st", Sdf.ValueTypeNames.TexCoord2fArray, interp)
        st.Set(Vt.Vec2fArray.FromNumpy(np.ascontiguousarray(uvv)))

    # Polypaint -> primvars:displayColor
    cd_class = _cd_source(geo)
    if cd_class:
        raw = (geo.pointFloatAttribValues("Cd") if cd_class == "point"
               else geo.vertexFloatAttribValues("Cd"))
        cd = np.asarray(raw, dtype=np.float32).reshape(-1, 3)
        interp = UsdGeom.Tokens.vertex if cd_class == "point" else UsdGeom.Tokens.faceVarying
        dc = api.CreatePrimvar("displayColor", Sdf.ValueTypeNames.Color3fArray, interp)
        dc.Set(Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(cd)))

    return mesh


def _author_material(stage, mesh, prim_name, tex_path):
    """Bind a UsdPreviewSurface to `mesh` whose diffuseColor is the texture,
    sampled through a UsdPrimvarReader on `st`."""
    look = "/root/Looks/" + prim_name
    material = UsdShade.Material.Define(stage, look)

    surface = UsdShade.Shader.Define(stage, look + "/Surface")
    surface.CreateIdAttr("UsdPreviewSurface")
    surface_out = surface.CreateOutput("surface", Sdf.ValueTypeNames.Token)

    reader = UsdShade.Shader.Define(stage, look + "/stReader")
    reader.CreateIdAttr("UsdPrimvarReader_float2")
    reader.CreateInput("varname", Sdf.ValueTypeNames.Token).Set("st")
    reader_out = reader.CreateOutput("result", Sdf.ValueTypeNames.Float2)

    texture = UsdShade.Shader.Define(stage, look + "/diffuseTex")
    texture.CreateIdAttr("UsdUVTexture")
    texture.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(tex_path.replace("\\", "/"))
    texture.CreateInput("st", Sdf.ValueTypeNames.Float2).ConnectToSource(reader_out)
    texture.CreateInput("wrapS", Sdf.ValueTypeNames.Token).Set("repeat")
    texture.CreateInput("wrapT", Sdf.ValueTypeNames.Token).Set("repeat")
    texture_out = texture.CreateOutput("rgb", Sdf.ValueTypeNames.Float3)

    surface.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).ConnectToSource(texture_out)
    material.CreateSurfaceOutput().ConnectToSource(surface_out)

    UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim())
    UsdShade.MaterialBindingAPI(mesh).Bind(material)
    return material


def author_usd(items, usd_path):
    """Author a USD stage from (name, hou.Geometry[, texture_path]) tuples.

    One Mesh prim per item under /root, with primvars:st and primvars:displayColor,
    plus a bound UsdPreviewSurface for any item that carries a texture.
    Returns a list of (prim_name, texture_path_or_None) in authored order.
    """
    usd_path = usd_path.replace("\\", "/")
    parent = os.path.dirname(usd_path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)

    # A prior import (LOP sublayer / usdimport) may still hold this layer open in the
    # USD registry, which makes CreateNew fail ("a layer already exists"). Reuse and
    # wipe the open layer in that case, so a re-Get updates it in place.
    layer = Sdf.Layer.Find(usd_path)
    if layer:
        layer.Clear()
        stage = Usd.Stage.Open(layer)
    else:
        if os.path.exists(usd_path):
            try:
                os.remove(usd_path)
            except OSError:
                pass
        stage = Usd.Stage.CreateNew(usd_path)

    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)       # ZBrush and Houdini are Y-up
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    root = UsdGeom.Xform.Define(stage, "/root")
    stage.SetDefaultPrim(root.GetPrim())

    used = set()
    prim_tex = []
    for item in items:
        name, geo = item[0], item[1]
        tex = item[2] if len(item) > 2 else None
        if geo is None or len(geo.iterPrims()) == 0:
            continue
        prim_name = _prim_name(name, used)
        mesh = _author_mesh(stage, "/root/" + prim_name, geo)
        if tex:
            _author_material(stage, mesh, prim_name, tex)
        prim_tex.append((prim_name, tex))

    stage.GetRootLayer().Save()
    return prim_tex


# --------------------------------------------------------------------------- #
# Bridge
# --------------------------------------------------------------------------- #
class Bridge(object):
    def __init__(self):
        self.cache = CACHE_DIR
        self.exe = ZBRUSH_EXE
        self.goz_dir = self.cache + "/goz"
        self.tex_dir = self.cache + "/tex"
        self.usd_path = self.cache + "/from_zbrush.usd"
        self.script_path = self.cache + "/get.txt"
        self.send_dir = self.cache + "/send"
        self.send_script_path = self.cache + "/send.txt"
        self.blender_dir = self.cache + "/blender"
        self.blender_cmd = self.blender_dir + "/cmd.json"
        self.blender_done = self.blender_dir + "/done.json"
        self.to_blender_usd = self.blender_dir + "/to_blender.usd"
        self.from_blender_usd = self.blender_dir + "/from_blender.usd"
        self.maya_dir = self.cache + "/maya"
        self.to_maya_usd = self.maya_dir + "/from_houdini.usd"
        self.from_maya_usd = self.maya_dir + "/to_houdini.usd"

    def _build_get_script(self):
        if EXPORT_TEXTURES:
            texture_block = _ZS_TEXTURE_BLOCK.replace("__TEX_DIR__", self.tex_dir + "/")
        else:
            texture_block = ""
        return (_ZS_EXPORT_GOZ
                .replace("__EXPORT_DIR__", self.goz_dir + "/")
                .replace("__TEXTURE_BLOCK__", texture_block)
                .replace("__SIGNAL__", GET_SIGNAL))

    def _texture_for(self, goz_path):
        """The .png exported next to a GoZ (same <index>_<name> stem), or None."""
        stem = os.path.splitext(os.path.basename(goz_path))[0]
        tex = os.path.join(self.tex_dir, stem + ".png").replace("\\", "/")
        return tex if os.path.isfile(tex) else None

    # ---- filesystem helpers ------------------------------------------------
    @staticmethod
    def _ensure_dir(path):
        if not os.path.isdir(path):
            os.makedirs(path)

    def _reset_dir(self, path):
        if os.path.isdir(path):
            rmtree(path, ignore_errors=True)
        os.makedirs(path)

    # ---- ZBrush 2026 sandbox-aware signalling ------------------------------
    def _signal_paths(self, name):
        filename = name + ".zvr"
        paths = [os.path.join(self.cache, filename)]
        for base in (os.getenv("LOCALAPPDATA"), os.getenv("TEMP"), os.getenv("TMP")):
            if not base:
                continue
            for pattern in (os.path.join(base, "Temp", "ZBrushData*"),
                            os.path.join(base, "ZBrushData*")):
                for folder in glob.glob(pattern):
                    paths.append(os.path.join(folder, filename))
        return paths

    def _clear_signal(self, name):
        for path in self._signal_paths(name):
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except OSError:
                pass

    def _wait_signal(self, name, timeout=SIGNAL_TIMEOUT):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for path in self._signal_paths(name):
                if os.path.isfile(path):
                    return True
            _qtw.QApplication.processEvents()      # keep Houdini responsive
            time.sleep(0.15)
        return False

    def _run_zscript(self, text, script_path):
        self._ensure_dir(self.cache)
        with open(script_path, "w") as fh:
            fh.write(text)
        # ZBrush is single-instance: launching it with a .txt makes the running
        # instance execute the ZScript. Detached so it outlives this call.
        QProcess.startDetached(self.exe, [script_path])

    # ---- GoZ filename <-> subtool name -------------------------------------
    @staticmethod
    def _sort_key(path):
        base = os.path.basename(path)
        m = re.match(r"(\d+)_", base)
        return (int(m.group(1)) if m else 0, base.lower())

    @staticmethod
    def _name_from_goz(path):
        base = os.path.splitext(os.path.basename(path))[0]
        return re.sub(r"^\d+_", "", base) or "subtool"

    # ---- Houdini-side import ----------------------------------------------
    def _get_target(self):
        """'sop' or 'lop' for where the Get result should land."""
        if GET_TARGET in ("sop", "lop"):
            return GET_TARGET
        for pane_type in (hou.paneTabType.NetworkEditor, hou.paneTabType.SceneViewer):
            try:
                pwd = hou.ui.paneTabOfType(pane_type).pwd()
                if pwd.childTypeCategory() == hou.lopNodeTypeCategory():
                    return "lop"
            except Exception:
                pass
        return "sop"

    def _import_usd_to_lop(self, usd_path, label="zbrush_usd", zup=False):
        """Sublayer the authored .usd into a LOP network (the active lopnet, else
        /stage). Materials/UV/hierarchy come in natively - no unpack needed."""
        lopnet = None
        try:
            pwd = hou.ui.paneTabOfType(hou.paneTabType.NetworkEditor).pwd()
            if pwd.childTypeCategory() == hou.lopNodeTypeCategory():
                lopnet = pwd
        except Exception:
            pass
        if lopnet is None:
            lopnet = hou.node("/stage") or hou.node("/").createNode("lopnet", "stage")

        sub = lopnet.createNode("sublayer", label)
        sub.parm("filepath1").set(usd_path.replace("\\", "/"))
        node = sub
        if zup:                                # Z-up stage -> rotate into Houdini's Y-up
            rot = sub.createOutputNode("xform", "z_up_to_y_up")
            try:
                rot.parm("primpattern").set("/*")
                rot.parmTuple("r").set((-90, 0, 0))
                node = rot
            except AttributeError:
                node = sub
        node.setDisplayFlag(True)
        node.setCurrent(True, True)
        try:
            lopnet.layoutChildren()
        except Exception:
            pass
        return node

    def _import_usd_to_sops(self, usd_path, prim_tex, label="from_zbrush", zup=False):
        obj = hou.node("/obj").createNode("geo", label)
        obj.moveToGoodPosition()
        imp = obj.createNode("usdimport", "usd_in")
        imp.parm("filepath1").set(usd_path.replace("\\", "/"))
        unpack = imp.createOutputNode("unpackusd", "to_polygons")
        unpack.parm("output").set(1)           # 1 = polygons (0 = packed prims)

        src = unpack
        if zup:                                # Z-up stage -> rotate into Houdini's Y-up
            rot = unpack.createOutputNode("xform", "z_up_to_y_up")
            rot.parmTuple("r").set((-90, 0, 0))
            src = rot

        terminal = self._apply_materials(obj, src, prim_tex)

        for flag in (terminal.setDisplayFlag, terminal.setRenderFlag):
            flag(True)
        terminal.setCurrent(True, True)
        try:
            obj.layoutChildren()
        except Exception:
            pass
        return terminal

    def _apply_materials(self, obj, unpack, prim_tex):
        """Build a Principled Shader per textured subtool and assign it by name.
        Returns the SOP that should carry the display flag (the material SOP if any
        textures were applied, otherwise the unpack node)."""
        textured = [(pn, tp) for pn, tp in prim_tex if tp and os.path.isfile(tp)]
        if not textured:
            return unpack

        matnet = hou.node("/obj").createNode("matnet", obj.name() + "_materials")
        matnet.moveToGoodPosition()
        shader_path = {}
        for prim_name, tex in textured:
            shader = matnet.createNode("principledshader::2.0", prim_name)
            shader.parm("basecolor_useTexture").set(1)
            shader.parm("basecolor_texture").set(tex.replace("\\", "/"))
            shader_path[prim_name] = shader.path()

        assign = unpack.createOutputNode("material", "assign_textures")
        assign.parm("num_materials").set(len(textured))
        for idx, (prim_name, _tex) in enumerate(textured, start=1):
            assign.parm("group%d" % idx).set("@name=%s" % prim_name)
            assign.parm("shop_materialpath%d" % idx).set(shader_path[prim_name])
        return assign

    # ---- public entry point ------------------------------------------------
    def get_from_zbrush(self):
        if not self.exe or not os.path.isfile(self.exe):
            hou.ui.displayMessage(
                "ZBrush.exe not found.\nSet ZBRUSH_EXEC_PATH in the usd_bridge package.\n"
                "Current value:\n%s" % self.exe,
                severity=hou.severityType.Error)
            return None

        self._reset_dir(self.goz_dir)
        if EXPORT_TEXTURES:
            self._reset_dir(self.tex_dir)

        self._clear_signal(GET_SIGNAL)
        self._run_zscript(self._build_get_script(), self.script_path)

        if not self._wait_signal(GET_SIGNAL):
            hou.ui.displayMessage(
                "ZBrush did not signal completion within %d s.\n"
                "Is ZBrush running with a tool loaded and at least one subtool visible?"
                % int(SIGNAL_TIMEOUT),
                severity=hou.severityType.Warning)
            return None

        gozs = glob.glob(self.goz_dir + "/*.GoZ") + glob.glob(self.goz_dir + "/*.goz")
        gozs = sorted(set(gozs), key=self._sort_key)
        if not gozs:
            hou.ui.displayMessage(
                "ZBrush finished but exported no subtools.\n"
                "Make sure at least one subtool's eye icon is on.",
                severity=hou.severityType.Warning)
            return None

        items = []
        for path in gozs:
            geo = hou.Geometry()
            try:
                geo.loadFromFile(path)
            except hou.OperationFailed:
                continue
            tex = self._texture_for(path) if EXPORT_TEXTURES else None
            items.append((self._name_from_goz(path), geo, tex))

        self._ensure_dir(self.cache)
        prim_tex = author_usd(items, self.usd_path)
        target = self._get_target()
        if target == "lop":
            node = self._import_usd_to_lop(self.usd_path)
        else:
            node = self._import_usd_to_sops(self.usd_path, prim_tex)

        n_tex = sum(1 for _pn, tp in prim_tex if tp)
        hou.ui.setStatusMessage(
            "USD Portal: imported %d subtool(s) (%d textured) into %s from ZBrush -> %s"
            % (len(prim_tex), n_tex, target.upper(), self.usd_path),
            severity=hou.severityType.ImportantMessage)
        return node

    # ======================================================================= #
    # Houdini -> ZBrush ("Send")
    # ======================================================================= #
    @staticmethod
    def _ancestor_object(node):
        n = node
        while n is not None and n.type().category() != hou.objNodeTypeCategory():
            n = n.parent()
        return n

    @staticmethod
    def _leaf(value):
        return (value or "").rsplit("/", 1)[-1] or "mesh"

    @staticmethod
    def _fs_name(name):
        return re.sub(r"[^A-Za-z0-9_.-]", "_", name).strip("_") or "mesh"

    def _active_geo_source(self):
        """Find what to send: (kind, node) where kind is 'sop' or 'lop'. Prefers the
        current selection, then the scene viewer."""
        for node in hou.selectedNodes():
            if node.type().category() == hou.lopNodeTypeCategory():
                return "lop", node
        for node in hou.selectedNodes():
            cat = node.type().category()
            if cat == hou.sopNodeTypeCategory():
                return "sop", node
            if cat == hou.objNodeTypeCategory() and node.displayNode() is not None:
                return "sop", node.displayNode()
        try:
            pwd = hou.ui.paneTabOfType(hou.paneTabType.SceneViewer).pwd()
            child = pwd.childTypeCategory()
            if child == hou.sopNodeTypeCategory() and pwd.displayNode():
                return "sop", pwd.displayNode()
            if child == hou.lopNodeTypeCategory() and pwd.displayNode():
                return "lop", pwd.displayNode()
        except Exception:
            pass
        return None, None

    @staticmethod
    def _usd_prereversed(geo):
        """True for geometry that came out of unpackusd: it keeps USD's right-handed
        vertex order (already opposite to native Houdini winding) and is tagged with
        the string prim attrib usdconfigreversepolygons = '1'."""
        attr = geo.findPrimAttrib("usdconfigreversepolygons")
        if attr is None:
            return False
        try:
            values = geo.primStringAttribValues("usdconfigreversepolygons")
        except hou.OperationFailed:
            return True                    # tagged, unexpected type -> assume reversed
        return any(v not in ("", "0") for v in values)

    def _reverse_winding(self, geo):
        """Reverse polygon winding (flip normals) via a temp Reverse SOP. Houdini and
        ZBrush wind faces oppositely, so OBJ meshes arrive inside-out without this."""
        self._ensure_dir(self.cache)
        tmp = self.cache + "/send_flip.bgeo"
        geo.saveToFile(tmp)
        work = hou.node("/obj").createNode("geo", "usd_bridge_flip_tmp")
        try:
            file_sop = work.createNode("file")
            file_sop.parm("file").set(tmp)
            return file_sop.createOutputNode("reverse").geometry().freeze()
        finally:
            work.destroy()

    def _split(self, geo, fallback_name):
        """Return [(subtool_name, hou.Geometry)] split by name/path attr (or one)."""
        if len(geo.iterPrims()) == 0:
            return []
        if SEND_FLIP_NORMALS and not self._usd_prereversed(geo):
            geo = self._reverse_winding(geo)
        attr = None
        if SEND_SPLIT == "auto":
            for candidate in ("name", "path"):
                if geo.findPrimAttrib(candidate) is not None:
                    attr = candidate
                    break
        if attr is None:
            return [(fallback_name, geo)]

        order = []
        for value in geo.primStringAttribValues(attr):
            if value not in order:
                order.append(value)
        if len(order) <= 1:
            return [(self._leaf(order[0]) if order else fallback_name, geo)]

        # Split with a temporary SOP network (detached geometry can't be subset in HOM).
        self._ensure_dir(self.cache)
        bgeo = self.cache + "/send_full.bgeo"
        geo.saveToFile(bgeo)
        work = hou.node("/obj").createNode("geo", "usd_bridge_split_tmp")
        try:
            file_sop = work.createNode("file")
            file_sop.parm("file").set(bgeo)
            pieces = []
            for value in order:
                blast = file_sop.createOutputNode("blast")
                blast.parm("group").set("@%s=%s" % (attr, value))
                blast.parm("grouptype").set(4)   # primitives
                blast.parm("negate").set(1)      # keep the matched prims
                pieces.append((self._leaf(value), blast.geometry().freeze()))
        finally:
            work.destroy()
        return pieces

    def _texture_for_geo(self, geo):
        """Diffuse texture assigned to this geometry, via the shop_materialpath prim
        attr -> a Principled Shader's basecolor_texture. Returns a path or None."""
        if geo.findPrimAttrib("shop_materialpath") is None:
            return None
        paths = [p for p in geo.primStringAttribValues("shop_materialpath") if p]
        if not paths:
            return None
        mat = hou.node(paths[0])
        if mat is None:
            return None
        parm = mat.parm("basecolor_texture")
        if parm is None:
            return None
        tex = parm.evalAsString().replace("\\", "/")
        return tex if tex and os.path.isfile(tex) else None

    @staticmethod
    def _copy_texture(src, dst, flip):
        """Copy a texture to dst, optionally flipped vertically (ZBrush's Texture Map
        expects the opposite V orientation). Returns True on success."""
        if flip:
            try:
                from PIL import Image
                Image.open(src).transpose(Image.FLIP_TOP_BOTTOM).save(dst)
                return True
            except Exception:
                pass   # Pillow missing/failed -> fall back to an unflipped copy
        try:
            copyfile(src, dst)
            return True
        except OSError:
            return False

    @staticmethod
    def _encode_cd(geo):
        """Re-encode point/vertex Cd for ZBrush's OBJ import, which decodes vertex
        colours as sRGB -> linear: pre-encoding linear -> sRGB cancels that out.
        Controlled by SEND_CD_COLORSPACE ('srgb' / 'linear' / 'none')."""
        if SEND_CD_COLORSPACE not in ("srgb", "linear"):
            return geo
        for cls in ("point", "vertex"):
            attr = (geo.findPointAttrib("Cd") if cls == "point"
                    else geo.findVertexAttrib("Cd"))
            if attr is None:
                continue
            raw = (geo.pointFloatAttribValues("Cd") if cls == "point"
                   else geo.vertexFloatAttribValues("Cd"))
            cd = np.clip(np.asarray(raw, dtype=np.float64), 0.0, 1.0)
            if SEND_CD_COLORSPACE == "srgb":      # linear -> sRGB
                cd = np.where(cd <= 0.0031308, cd * 12.92,
                              1.055 * np.power(cd, 1.0 / 2.4) - 0.055)
            else:                                  # sRGB -> linear
                cd = np.where(cd <= 0.04045, cd / 12.92,
                              np.power((cd + 0.055) / 1.055, 2.4))
            values = np.ascontiguousarray(cd, dtype=np.float32).ravel()
            if cls == "point":
                geo.setPointFloatAttribValues("Cd", values.tolist())
            else:
                geo.setVertexFloatAttribValues("Cd", values.tolist())
        return geo

    def _drop_hidden_prims(self, geo):
        """Visibility filter for SOPs: drop prims in the _3d_hidden_primitives group
        (Houdini's viewport-hide convention) before sending."""
        group = geo.findPrimGroup("_3d_hidden_primitives")
        if group is None or len(group.prims()) == 0:
            return geo
        self._ensure_dir(self.cache)
        tmp = self.cache + "/send_vis.bgeo"
        geo.saveToFile(tmp)
        work = hou.node("/obj").createNode("geo", "usd_bridge_vis_tmp")
        try:
            file_sop = work.createNode("file")
            file_sop.parm("file").set(tmp)
            blast = file_sop.createOutputNode("blast")
            blast.parm("group").set("_3d_hidden_primitives")
            blast.parm("grouptype").set(4)
            return blast.geometry().freeze()
        finally:
            work.destroy()

    def _pieces_from_sop(self, sop):
        obj = self._ancestor_object(sop)
        geo = sop.geometry().freeze()
        if obj is not None:                      # bake to world space
            geo.transform(obj.worldTransform())
        geo = self._drop_hidden_prims(geo)
        return self._split(geo, obj.name() if obj else "mesh")

    @staticmethod
    def _stage_textures(stage):
        """Map prim leaf name -> diffuse texture path, from each Mesh's bound
        UsdPreviewSurface (diffuseColor <- UsdUVTexture file input)."""
        textures = {}
        for prim in stage.Traverse():
            if prim.GetTypeName() != "Mesh":
                continue
            try:
                material = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()[0]
                if not (material and material.GetPrim().IsValid()):
                    continue
                surface = material.ComputeSurfaceSource()[0]
                if not surface:
                    continue
                diffuse = surface.GetInput("diffuseColor")
                sources = diffuse.GetConnectedSources()[0] if diffuse else ()
                if not sources:
                    continue
                shader = UsdShade.Shader(sources[0].source.GetPrim())
                asset = shader.GetInput("file").Get()
                path = (getattr(asset, "resolvedPath", "") or
                        getattr(asset, "path", "") or "").replace("\\", "/")
                if path and os.path.isfile(path):
                    textures[prim.GetName()] = path
            except Exception:
                continue
        return textures

    @staticmethod
    def _promote_color_primvar(stage):
        """Blender exports a mesh colour attribute under its own name (e.g.
        primvars:Color, color4f) instead of the USD-conventional displayColor.
        Wherever displayColor is missing/empty, promote the first colour primvar to
        primvars:displayColor (color3f, same interpolation, alpha dropped). Returns
        the number of meshes promoted."""
        promoted = 0
        for prim in stage.Traverse():
            if prim.GetTypeName() != "Mesh":
                continue
            try:
                api = UsdGeom.PrimvarsAPI(prim)
                dc = api.GetPrimvar("displayColor")
                if dc and dc.HasValue():
                    existing = dc.ComputeFlattened()
                    if existing is not None and len(existing) > 0:
                        continue
                candidates = []
                for pv in api.GetPrimvars():
                    name = pv.GetPrimvarName()
                    if name in ("displayColor", "displayOpacity") or not pv.HasValue():
                        continue
                    type_name = str(pv.GetTypeName())
                    if type_name.startswith("color3f") or type_name.startswith("color4f"):
                        candidates.append(pv)
                if not candidates:
                    continue
                # prefer Blender's default attribute name, else the first colour primvar
                cand = next((pv for pv in candidates
                             if pv.GetPrimvarName().lower() in ("color", "cd")),
                            candidates[0])
                values = cand.ComputeFlattened()
                if values is None or len(values) == 0:
                    continue
                arr = np.array(values, dtype=np.float32).reshape(len(values), -1)[:, :3]
                out = api.CreatePrimvar("displayColor", Sdf.ValueTypeNames.Color3fArray,
                                        cand.GetInterpolation())
                out.Set(Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(arr)))
                promoted += 1
            except Exception:
                continue
        return promoted

    @staticmethod
    def _wire_displaycolor_materials(stage):
        """Blender's USD export drops the Color Attribute node when converting a
        material, leaving diffuseColor unconnected while the mesh still carries the
        displayColor primvar - the bound material then shades flat/black. Wire a
        UsdPrimvarReader_float3(displayColor) into every such material. Returns the
        number of materials fixed."""
        fixed = 0
        for prim in stage.Traverse():
            if prim.GetTypeName() != "Mesh":
                continue
            try:
                pv = UsdGeom.PrimvarsAPI(prim).GetPrimvar("displayColor")
                if not pv or not pv.HasValue():
                    continue
                material = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()[0]
                if not (material and material.GetPrim().IsValid()):
                    continue
                surface = material.ComputeSurfaceSource()[0]
                if not surface:
                    continue
                diffuse = surface.GetInput("diffuseColor")
                if diffuse and diffuse.GetConnectedSources()[0]:
                    continue                      # already driven (e.g. a texture)
                reader = UsdShade.Shader.Define(
                    stage, material.GetPath().AppendChild("displayColorReader"))
                reader.CreateIdAttr("UsdPrimvarReader_float3")
                reader.CreateInput("varname", Sdf.ValueTypeNames.Token).Set("displayColor")
                out = reader.CreateOutput("result", Sdf.ValueTypeNames.Float3)
                surface.CreateInput("diffuseColor",
                                    Sdf.ValueTypeNames.Color3f).ConnectToSource(out)
                fixed += 1
            except Exception:
                continue
        return fixed

    def _pieces_from_lop(self, lop):
        self._ensure_dir(self.cache)
        flat = self.cache + "/send_from_lop.usd"
        stage = lop.stage()
        stage.Export(flat)
        self._lop_textures = self._stage_textures(stage)
        tmp = hou.node("/obj").createNode("geo", "usd_bridge_lop_tmp")
        try:
            imp = tmp.createNode("usdimport")
            imp.parm("filepath1").set(flat)
            unpack = imp.createOutputNode("unpackusd")
            unpack.parm("output").set(1)         # polygons
            geo = unpack.geometry().freeze()
        finally:
            tmp.destroy()
        return self._split(geo, "stage")

    def _build_send_script(self, files, texfiles, texnames):
        rows = []
        for i, mesh in enumerate(files):
            rows.append('[VarSet, files({0}), "{1}"]'.format(i, mesh.replace("\\", "/")))
            rows.append('[VarSet, texfiles({0}), "{1}"]'.format(i, texfiles[i].replace("\\", "/")))
            rows.append('[VarSet, texnames({0}), "{1}"]'.format(i, texnames[i]))
        return (_ZS_SEND
                .replace("__COUNT__", str(len(files)))
                .replace("__FILE_LIST__", "\n".join(rows))
                .replace("__TEX_K__", _ZS_TEX_APPLY.replace("INDEX", "k"))
                .replace("__TEX_0__", _ZS_TEX_APPLY.replace("INDEX", "0"))
                .replace("__APPEND__", "1" if SEND_TO_CURRENT_TOOL else "0")
                .replace("__SIGNAL__", SEND_SIGNAL))

    def send_to_zbrush(self):
        if not self.exe or not os.path.isfile(self.exe):
            hou.ui.displayMessage(
                "ZBrush.exe not found.\nSet ZBRUSH_EXEC_PATH in the usd_bridge package.\n"
                "Current value:\n%s" % self.exe,
                severity=hou.severityType.Error)
            return

        kind, node = self._active_geo_source()
        if node is None:
            hou.ui.displayMessage(
                "Nothing to send. Select a SOP or LOP node (or dive into a geo object) "
                "and try again.",
                severity=hou.severityType.Warning)
            return

        self._lop_textures = {}   # filled by _pieces_from_lop

        try:
            pieces = (self._pieces_from_sop(node) if kind == "sop"
                      else self._pieces_from_lop(node))
        except Exception as exc:
            hou.ui.displayMessage("Could not read geometry to send:\n%s" % exc,
                                  severity=hou.severityType.Error)
            return

        if not pieces:
            hou.ui.displayMessage("No polygon geometry found to send.",
                                  severity=hou.severityType.Warning)
            return

        self._reset_dir(self.send_dir)
        send_id = uuid.uuid4().hex[:8]
        files, texfiles, texnames = [], [], []
        for idx, (name, geo) in enumerate(pieces):
            if SEND_POLYPAINT:
                self._encode_cd(geo)          # counter ZBrush's sRGB decode
            else:
                for finder in (geo.findPointAttrib, geo.findVertexAttrib):
                    attr = finder("Cd")
                    if attr is not None:
                        attr.destroy()
            mesh_path = "%s/%d_%s.%s" % (self.send_dir, idx, self._fs_name(name), SEND_FORMAT)
            geo.saveToFile(mesh_path)
            files.append(mesh_path)

            tex = None
            if SEND_TEXTURES:
                tex = self._texture_for_geo(geo) or self._lop_textures.get(name)
            copied = ""
            base = ""
            if tex:
                base = "ubtex_%s_%d" % (send_id, idx)      # unique -> predictable ZBrush name
                dst = "%s/%s%s" % (self.send_dir, base, os.path.splitext(tex)[1] or ".png")
                if self._copy_texture(tex, dst, SEND_FLIP_TEXTURE_V):
                    copied = dst
                else:
                    base = ""
            texfiles.append(copied)
            texnames.append(base)

        self._clear_signal(SEND_SIGNAL)
        self._run_zscript(self._build_send_script(files, texfiles, texnames),
                          self.send_script_path)

        if not self._wait_signal(SEND_SIGNAL):
            hou.ui.displayMessage(
                "ZBrush did not confirm the import within %d s.\n"
                "If ZBrush wasn't running, start it and try again; otherwise it may "
                "still be importing." % int(SIGNAL_TIMEOUT),
                severity=hou.severityType.Warning)
            return

        hou.ui.setStatusMessage(
            "USD Portal: sent %d subtool(s) (%d textured) from %s to ZBrush"
            % (len(files), sum(1 for t in texnames if t), kind.upper()),
            severity=hou.severityType.ImportantMessage)

    # ======================================================================= #
    # Houdini <-> Blender / Maya (USD + JSON command files served by their agents)
    # ======================================================================= #
    def _app_dir(self, app):
        return self.blender_dir if app == "blender" else self.maya_dir

    def _write_app_cmd(self, app, op, payload=None):
        """Atomically publish a command for an app agent. Returns its id."""
        xdir = self._app_dir(app)
        self._ensure_dir(xdir)
        done = xdir + "/done.json"
        try:
            if os.path.isfile(done):
                os.remove(done)                    # drop a stale answer
        except OSError:
            pass
        cmd = dict(payload or {})
        cmd["op"] = op
        cmd["id"] = uuid.uuid4().hex
        target = xdir + "/cmd.json"
        tmp = target + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(cmd, fh)
        os.replace(tmp, target)
        return cmd["id"]

    def _wait_app_done(self, app, cmd_id, timeout=BLENDER_TIMEOUT):
        done = self._app_dir(app) + "/done.json"
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
            _qtw.QApplication.processEvents()
            time.sleep(0.15)
        return None

    # kept for compatibility (tests use these against overridden blender paths)
    def _write_blender_cmd(self, op, payload=None):
        return self._write_app_cmd("blender", op, payload)

    def _wait_blender_done(self, cmd_id, timeout=BLENDER_TIMEOUT):
        return self._wait_app_done("blender", cmd_id, timeout)

    def _app_no_answer(self, app):
        agent = ("the 'USD Portal' add-on" if app == "blender"
                 else "the usd_bridge_maya script")
        hou.ui.displayMessage(
            "%s did not respond within %d s.\n"
            "Is it running with %s enabled?"
            % (app.capitalize(), int(BLENDER_TIMEOUT), agent),
            severity=hou.severityType.Warning)

    @staticmethod
    def _prune_invisible(usd_path):
        """Deactivate invisible prims in an exported stage so hidden LOP geometry
        never reaches the other app. Returns the number of pruned prims."""
        layer = Sdf.Layer.Find(usd_path)
        if layer:
            layer.Reload(force=True)
        stage = Usd.Stage.Open(usd_path)
        doomed = []
        for prim in stage.Traverse():
            img = UsdGeom.Imageable(prim)
            if not img:
                continue
            try:
                if img.ComputeVisibility() == UsdGeom.Tokens.invisible:
                    doomed.append(prim.GetPath())
            except Exception:
                continue
        for path in doomed:
            stage.GetPrimAtPath(path).SetActive(False)
        if doomed:
            stage.GetRootLayer().Save()
        return len(doomed)

    def _send_usd_to_app(self, app):
        """Send the selected SOP / LOP geometry to Blender or Maya as USD."""
        kind, node = self._active_geo_source()
        if node is None:
            hou.ui.displayMessage(
                "Nothing to send. Select a SOP or LOP node (or dive into a geo object) "
                "and try again.",
                severity=hou.severityType.Warning)
            return

        usd_path = self.to_blender_usd if app == "blender" else self.to_maya_usd
        self._ensure_dir(self._app_dir(app))
        try:
            if kind == "lop":
                # Flatten the node's stage; materials/UV/Cd travel natively.
                layer = Sdf.Layer.Find(usd_path)
                if layer:                       # path may be held open by a prior run
                    layer.Clear()
                node.stage().Export(usd_path)
                self._prune_invisible(usd_path)   # visibility filter
            else:
                pieces = self._pieces_from_sop(node)
                if not pieces:
                    hou.ui.displayMessage("No polygon geometry found to send.",
                                          severity=hou.severityType.Warning)
                    return
                items = [(name, geo, self._texture_for_geo(geo)) for name, geo in pieces]
                author_usd(items, usd_path)
        except Exception as exc:
            hou.ui.displayMessage("Could not build the USD to send:\n%s" % exc,
                                  severity=hou.severityType.Error)
            return

        cmd_id = self._write_app_cmd(app, "import_usd", {"usd": usd_path})
        done = self._wait_app_done(app, cmd_id)
        if done is None:
            self._app_no_answer(app)
            return
        if not done.get("ok"):
            hou.ui.displayMessage("%s import failed:\n%s"
                                  % (app.capitalize(), done.get("error", "?")),
                                  severity=hou.severityType.Error)
            return
        hou.ui.setStatusMessage(
            "USD Portal: sent %s to %s (%s object(s) imported)"
            % (kind.upper(), app.capitalize(), done.get("objects", "?")),
            severity=hou.severityType.ImportantMessage)

    def send_to_blender(self):
        return self._send_usd_to_app("blender")

    def send_to_maya(self):
        return self._send_usd_to_app("maya")

    def get_from_maya(self):
        return self._get_usd_from_app("maya")

    def get_from_blender(self):
        return self._get_usd_from_app("blender")

    def _get_usd_from_app(self, app):
        """Ask the app to export its (visible) selection and import the USD."""
        usd_path = self.from_blender_usd if app == "blender" else self.from_maya_usd
        label = "blender_usd" if app == "blender" else "maya_usd"
        sop_label = "from_" + app
        self._ensure_dir(self._app_dir(app))
        cmd_id = self._write_app_cmd(app, "export_usd", {"usd": usd_path})
        done = self._wait_app_done(app, cmd_id)
        if done is None:
            self._app_no_answer(app)
            return None
        if not done.get("ok"):
            hou.ui.displayMessage("%s export failed:\n%s"
                                  % (app.capitalize(), done.get("error", "?")),
                                  severity=hou.severityType.Error)
            return None
        if not os.path.isfile(usd_path):
            hou.ui.displayMessage("%s reported success but wrote no USD file."
                                  % app.capitalize(),
                                  severity=hou.severityType.Warning)
            return None
        return self._import_app_usd(usd_path, label, sop_label, app)

    def _import_app_usd(self, usd_path, label, sop_label, app):
        # A sublayer from a previous Get may still hold this identifier in the USD
        # layer registry; without a FORCED reload, Usd.Stage.Open would return the
        # stale in-memory content instead of the app's fresh write (plain Reload()
        # trusts mtimes and can miss a same-second rewrite).
        layer = Sdf.Layer.Find(usd_path)
        if layer:
            layer.Reload(force=True)

        stage = Usd.Stage.Open(usd_path)
        zup = str(UsdGeom.GetStageUpAxis(stage)) == "Z"
        prim_tex = sorted(self._stage_textures(stage).items())
        dirty = self._promote_color_primvar(stage)          # <set name> -> displayColor
        dirty += self._wire_displaycolor_materials(stage)   # displayColor -> diffuse
        if dirty:
            stage.GetRootLayer().Save()       # persist for the sublayer/usdimport

        target = self._get_target()
        if target == "lop":
            node = self._import_usd_to_lop(usd_path, label=label, zup=zup)
        else:
            node = self._import_usd_to_sops(usd_path, prim_tex,
                                            label=sop_label, zup=zup)
        hou.ui.setStatusMessage(
            "USD Portal: imported %s geometry into %s (%d textured material(s))"
            % (app.capitalize(), target.upper(), len([1 for _n, t in prim_tex if t])),
            severity=hou.severityType.ImportantMessage)
        return node


# --------------------------------------------------------------------------- #
# Shelf entry points
# --------------------------------------------------------------------------- #
_bridge = None


def bridge():
    global _bridge
    if _bridge is None:
        _bridge = Bridge()
    return _bridge


def get_from_zbrush():
    return bridge().get_from_zbrush()


def send_to_zbrush():
    return bridge().send_to_zbrush()


def send_to_blender():
    return bridge().send_to_blender()


def get_from_blender():
    return bridge().get_from_blender()


def send_to_maya():
    return bridge().send_to_maya()


def get_from_maya():
    return bridge().get_from_maya()

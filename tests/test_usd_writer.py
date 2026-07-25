"""
Head-less round-trip test for usd_bridge, runnable without ZBrush.

    "<HFS>/bin/hython.exe" tests/test_usd_writer.py

Builds synthetic hou.Geometry that mimics GoZ imports (vertex `uv` + point `Cd`),
authors a USD stage (with a bound UsdPreviewSurface for the textured subtool),
verifies it with pxr, re-imports through the usdimport -> unpackusd(polygons) chain,
and exercises the SOP-side Principled Shader assignment.
"""
import binascii
import json
import os
import sys
import tempfile

import hou

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "package", "usd_bridge", "python3.13libs"))

import usd_bridge  # noqa: E402
from pxr import Usd, UsdGeom, UsdShade, Sdf  # noqa: E402


_failures = []


def check(cond, msg):
    print(("  ok  " if cond else " FAIL ") + msg)
    if not cond:
        _failures.append(msg)


def _make_subtool(name, tx, cr, cg):
    """A box with unwrapped UVs and a flat polypaint colour, like one GoZ subtool."""
    geo = hou.node("/obj").createNode("geo", "src_" + name)
    box = geo.createNode("box")
    box.parmTuple("t").set((tx, 0, 0))
    uv = box.createOutputNode("uvunwrap")
    col = uv.createOutputNode("color")
    col.parm("colorr").set(cr)
    col.parm("colorg").set(cg)
    return col.geometry().freeze()


def _write_stub_png(path):
    """A real (tiny) 1x1 PNG so the texture asset path resolves on disk."""
    png = ("89504e470d0a1a0a0000000d494844520000000100000001080200000000"
           "907753de0000000c49444154789c63f8cfc0f01f0005010102a5f645b400"
           "00000049454e44ae426082")
    with open(path, "wb") as fh:
        fh.write(binascii.unhexlify(png))


def main():
    tmp = tempfile.mkdtemp(prefix="usd_bridge_test_").replace(os.sep, "/")
    usd_path = tmp + "/roundtrip.usd"
    tex_path = tmp + "/head_diffuse.png"
    _write_stub_png(tex_path)

    items = [
        ("Head", _make_subtool("Head", 0.0, 0.9, 0.1), tex_path),
        ("Body Mesh", _make_subtool("Body", 3.0, 0.1, 0.9), None),   # space -> sanitised
    ]

    # --- author -----------------------------------------------------------
    prim_tex = usd_bridge.author_usd(items, usd_path)
    check(os.path.exists(usd_path), "stage written to disk")
    check(prim_tex == [("Head", tex_path), ("Body_Mesh", None)],
          "author_usd returns prim/texture map: %s" % prim_tex)

    # --- verify geometry with pxr ----------------------------------------
    stage = Usd.Stage.Open(usd_path)
    check(stage is not None, "pxr can open the stage")
    check(str(stage.GetDefaultPrim().GetPath()) == "/root", "defaultPrim is /root")
    check(UsdGeom.GetStageUpAxis(stage) == "Y", "up axis is Y")

    meshes = [p for p in stage.Traverse() if p.GetTypeName() == "Mesh"]
    check(len(meshes) == 2, "two Mesh prims authored")
    names = sorted(m.GetName() for m in meshes)
    check(names == ["Body_Mesh", "Head"], "prim names sanitised/unique: %s" % names)

    for m in meshes:
        pvs = {pv.GetPrimvarName(): pv.GetInterpolation()
               for pv in UsdGeom.PrimvarsAPI(m).GetPrimvars()}
        check(pvs.get("st") == "faceVarying", "%s: st is faceVarying" % m.GetName())
        check(pvs.get("displayColor") == "vertex", "%s: displayColor is vertex" % m.GetName())

    # --- verify material --------------------------------------------------
    head = UsdGeom.Mesh.Get(stage, "/root/Head")
    mat = UsdShade.MaterialBindingAPI(head).ComputeBoundMaterial()[0]
    check(mat.GetPrim().IsValid(), "Head has a bound material")
    surf = mat.ComputeSurfaceSource()[0]
    check(surf.GetIdAttr().Get() == "UsdPreviewSurface", "Head surface is UsdPreviewSurface")
    src = surf.GetInput("diffuseColor").GetConnectedSources()[0][0]
    texsh = UsdShade.Shader(src.source.GetPrim())
    check(texsh.GetIdAttr().Get() == "UsdUVTexture", "diffuseColor driven by UsdUVTexture")
    check(texsh.GetInput("file").Get().path == tex_path, "texture file wired: %s"
          % texsh.GetInput("file").Get().path)

    body = UsdGeom.Mesh.Get(stage, "/root/Body_Mesh")
    bmat = UsdShade.MaterialBindingAPI(body).ComputeBoundMaterial()[0]
    check(not bmat.GetPrim().IsValid(), "Body_Mesh has no material (no texture)")

    # --- re-import through the shelf-tool chain --------------------------
    obj = hou.node("/obj").createNode("geo", "reimport")
    imp = obj.createNode("usdimport")
    imp.parm("filepath1").set(usd_path)
    unpack = imp.createOutputNode("unpackusd")
    unpack.parm("output").set(1)      # polygons
    g = unpack.geometry()

    check(len(g.iterPrims()) == 12, "re-imported 12 polygons (2 boxes)")
    check(g.findVertexAttrib("uv") is not None, "uv survived (st -> uv)")
    check(g.findPointAttrib("Cd") is not None, "Cd survived (displayColor -> Cd)")
    paths = sorted(set(g.primStringAttribValues("path"))) if g.findPrimAttrib("path") else []
    check(paths == ["/root/Body_Mesh", "/root/Head"], "hierarchy path attr intact: %s" % paths)

    # --- LOP import (sublayer) -------------------------------------------
    lopsub = usd_bridge.Bridge()._import_usd_to_lop(usd_path)
    lstage = lopsub.stage()
    lhead = UsdGeom.Mesh.Get(lstage, "/root/Head")
    check(bool(lhead), "lop: /root/Head present in sublayered stage")
    lmat = UsdShade.MaterialBindingAPI(lhead).ComputeBoundMaterial()[0] if lhead else None
    check(bool(lmat) and lmat.GetPrim().IsValid(), "lop: material binding intact in stage")

    saved_target = usd_bridge.GET_TARGET
    try:
        usd_bridge.GET_TARGET = "lop"
        check(usd_bridge.Bridge()._get_target() == "lop", "lop: GET_TARGET='lop' honoured")
        usd_bridge.GET_TARGET = "sop"
        check(usd_bridge.Bridge()._get_target() == "sop", "lop: GET_TARGET='sop' honoured")
    finally:
        usd_bridge.GET_TARGET = saved_target

    # re-Get: author the same path again while the sublayer above still holds it open
    reauth = usd_bridge.author_usd([("Head", _make_subtool("Hr", 0, 1, 1), None)], usd_path)
    check(reauth == [("Head", None)], "re-author held-open path: no 'layer already exists' crash")
    rstage = Usd.Stage.Open(usd_path)   # hold the ref so the prim doesn't expire
    check(bool(UsdGeom.Mesh.Get(rstage, "/root/Head")), "re-authored stage still valid")

    # --- SOP-side material assignment ------------------------------------
    term = usd_bridge.Bridge()._apply_materials(obj, unpack, prim_tex)
    check(term.type().name() == "material", "material SOP created")
    shader = hou.node("/obj/reimport_materials/Head")
    check(shader is not None, "Head Principled Shader created")
    check(shader is not None and shader.evalParm("basecolor_texture") == tex_path,
          "shader points at the texture")
    check(term.parm("group1").eval() == "@name=Head", "material assigned by @name")

    # --- V-flip toggle affects st ----------------------------------------
    def _st_v(path):
        s = Usd.Stage.Open(path)
        m = UsdGeom.Mesh.Get(s, "/root/H")
        return [round(float(v[1]), 4) for v in UsdGeom.PrimvarsAPI(m).GetPrimvar("st").Get()]

    saved = usd_bridge.FLIP_TEXTURE_V
    try:
        usd_bridge.FLIP_TEXTURE_V = False
        usd_bridge.author_usd([("H", _make_subtool("Hn", 0, 1, 1), None)], tmp + "/noflip.usd")
        usd_bridge.FLIP_TEXTURE_V = True
        usd_bridge.author_usd([("H", _make_subtool("Hf", 0, 1, 1), None)], tmp + "/flip.usd")
    finally:
        usd_bridge.FLIP_TEXTURE_V = saved
    nf, fl = _st_v(tmp + "/noflip.usd"), _st_v(tmp + "/flip.usd")
    check(len(nf) == len(fl) and all(abs((1.0 - a) - b) < 1e-4 for a, b in zip(nf, fl)),
          "FLIP_TEXTURE_V flips st V (v -> 1-v)")

    # --- SEND: split into subtools + build import script -----------------
    bridge = usd_bridge.Bridge()
    ssrc = hou.node("/obj").createNode("geo", "send_src")
    pa = ssrc.createNode("box").createOutputNode("uvunwrap").createOutputNode("name")
    pa.parm("name1").set("Alpha")
    pb = ssrc.createNode("box")
    pb.parmTuple("t").set((3, 0, 0))
    pbn = pb.createOutputNode("uvunwrap").createOutputNode("name")
    pbn.parm("name1").set("Beta")
    merged = ssrc.createNode("merge")
    merged.setInput(0, pa)
    merged.setInput(1, pbn)

    pieces = bridge._split(merged.geometry().freeze(), "fallback")
    check(sorted(n for n, _ in pieces) == ["Alpha", "Beta"],
          "send: split by name -> %s" % sorted(n for n, _ in pieces))
    check(all(len(g.iterPrims()) == 6 for _, g in pieces), "send: each piece keeps 6 prims")
    check(all(g.findVertexAttrib("uv") or g.findPointAttrib("uv") for _, g in pieces),
          "send: uv preserved on each piece")

    solo = bridge._split(ssrc.createNode("box").createOutputNode("uvunwrap").geometry().freeze(),
                         "solo")
    check(len(solo) == 1 and solo[0][0] == "solo", "send: no name attr -> single piece")

    sdir = tempfile.mkdtemp(prefix="ub_send_").replace(os.sep, "/")
    files = []
    for i, (nm, g) in enumerate(pieces):
        fp = "%s/%d_%s.obj" % (sdir, i, bridge._fs_name(nm))
        g.saveToFile(fp)
        files.append(fp)
    check(all(os.path.exists(f) for f in files), "send: OBJ pieces written to disk")
    texfiles = [sdir + "/ubtex_abc_0.png", ""]
    texnames = ["ubtex_abc_0", ""]
    script = bridge._build_send_script(files, texfiles, texnames)
    _placeholders = ("__COUNT__", "__FILE_LIST__", "__TEX_K__", "__TEX_0__",
                     "__APPEND__", "__SIGNAL__", "INDEX")
    check(not any(p in script for p in _placeholders),
          "send: script has no leftover placeholders")
    check("Tool:Import" in script and "SubTool:Duplicate" in script,
          "send: script imports + duplicates subtools")
    check('[VarDef, count, 2]' in script, "send: file count wired (2)")
    check("Tool:Texture Map:TextureMap" in script and 'StrMerge, "PopUp:"' in script,
          "send: texture import + assign wired")
    check('[VarSet, texnames(0), "ubtex_abc_0"]' in script, "send: texture name wired")

    # texture extraction from an assigned Principled Shader
    txmat = hou.node("/obj").createNode("matnet", "sendmat").createNode("principledshader::2.0", "tx")
    txmat.parm("basecolor_texture").set(tex_path)
    txgeo = (hou.node("/obj").createNode("geo", "txgeo").createNode("box")
             .createOutputNode("material"))
    txgeo.parm("num_materials").set(1)
    txgeo.parm("group1").set("*")
    txgeo.parm("shop_materialpath1").set(txmat.path())
    check(bridge._texture_for_geo(txgeo.geometry().freeze()) == tex_path,
          "send: texture extracted from Principled Shader")

    # Cd sRGB pre-encode for ZBrush + LOP-stage texture extraction
    cgeo = hou.node("/obj").createNode("geo", "cd").createNode("box").createOutputNode("color")
    cgeo.parm("colorr").set(0.5); cgeo.parm("colorg").set(0.25); cgeo.parm("colorb").set(0.0)
    cg = cgeo.geometry().freeze()
    usd_bridge.Bridge()._encode_cd(cg)
    enc = cg.pointFloatAttribValues("Cd")[:3]
    check(abs(enc[0] - 0.7354) < 1e-3 and abs(enc[1] - 0.5371) < 1e-3 and enc[2] == 0.0,
          "send: Cd pre-encoded linear->sRGB (%.3f, %.3f, %.3f)" % enc)

    # displayColor wiring fix (Blender drops the Color Attribute node on export)
    wstage = Usd.Stage.CreateNew(tmp + "/wire.usd")
    UsdGeom.SetStageUpAxis(wstage, UsdGeom.Tokens.y)
    wroot = UsdGeom.Xform.Define(wstage, "/root")
    wstage.SetDefaultPrim(wroot.GetPrim())
    usd_bridge._author_mesh(wstage, "/root/VC", _make_subtool("Wv", 0, 1, 0))
    wmat = UsdShade.Material.Define(wstage, "/root/Looks/VC")
    wsurf = UsdShade.Shader.Define(wstage, "/root/Looks/VC/S")
    wsurf.CreateIdAttr("UsdPreviewSurface")
    wmat.CreateSurfaceOutput().ConnectToSource(
        wsurf.CreateOutput("surface", Sdf.ValueTypeNames.Token))
    wmesh = UsdGeom.Mesh.Get(wstage, "/root/VC")
    UsdShade.MaterialBindingAPI.Apply(wmesh.GetPrim())
    UsdShade.MaterialBindingAPI(wmesh).Bind(wmat)
    n1 = usd_bridge.Bridge._wire_displaycolor_materials(wstage)
    srcs = wsurf.GetInput("diffuseColor").GetConnectedSources()[0]
    drv = (UsdShade.Shader(srcs[0].source.GetPrim()).GetIdAttr().Get() if srcs else None)
    check(n1 == 1 and drv == "UsdPrimvarReader_float3",
          "blender: displayColor wired into material (fixed=%d, driver=%s)" % (n1, drv))
    check(usd_bridge.Bridge._wire_displaycolor_materials(wstage) == 0,
          "blender: displayColor wiring is idempotent")

    # Blender names the colour primvar after its attribute (primvars:Color, color4f)
    # -> promote to displayColor, then wiring + SOP Cd conversion must work
    from pxr import Vt, Gf
    pstage = Usd.Stage.CreateNew(tmp + "/promote.usd")
    UsdGeom.SetStageUpAxis(pstage, UsdGeom.Tokens.y)
    proot = UsdGeom.Xform.Define(pstage, "/root")
    pstage.SetDefaultPrim(proot.GetPrim())
    usd_bridge._author_mesh(pstage, "/root/PC", _make_subtool("Pv", 0, 1, 0))
    pmesh = UsdGeom.Mesh.Get(pstage, "/root/PC")
    papi = UsdGeom.PrimvarsAPI(pmesh)
    papi.RemovePrimvar("displayColor")                      # Blender-style: no displayColor
    npts = len(pmesh.GetPointsAttr().Get())
    c4 = papi.CreatePrimvar("Color", Sdf.ValueTypeNames.Color4fArray, UsdGeom.Tokens.vertex)
    c4.Set(Vt.Vec4fArray([Gf.Vec4f(0.42, 0.17, 0.1, 1.0)] * npts))
    pmat = UsdShade.Material.Define(pstage, "/root/Looks/PC")
    psurf = UsdShade.Shader.Define(pstage, "/root/Looks/PC/S")
    psurf.CreateIdAttr("UsdPreviewSurface")
    pmat.CreateSurfaceOutput().ConnectToSource(
        psurf.CreateOutput("surface", Sdf.ValueTypeNames.Token))
    UsdShade.MaterialBindingAPI.Apply(pmesh.GetPrim())
    UsdShade.MaterialBindingAPI(pmesh).Bind(pmat)

    np_ = usd_bridge.Bridge._promote_color_primvar(pstage)
    nw_ = usd_bridge.Bridge._wire_displaycolor_materials(pstage)
    ndc = papi.GetPrimvar("displayColor")
    vals = ndc.Get() if ndc else []
    check(np_ == 1 and nw_ == 1 and bool(ndc) and str(ndc.GetTypeName()).startswith("color3f")
          and abs(vals[0][0] - 0.42) < 1e-4,
          "blender: primvars:Color promoted to displayColor + wired (%d/%d)" % (np_, nw_))
    pstage.GetRootLayer().Save()
    pobj = hou.node("/obj").createNode("geo", "promote_check")
    pimp = pobj.createNode("usdimport"); pimp.parm("filepath1").set(tmp + "/promote.usd")
    pup = pimp.createOutputNode("unpackusd"); pup.parm("output").set(1)
    pg = pup.geometry()
    check(pg.findPointAttrib("Cd") is not None or pg.findVertexAttrib("Cd") is not None,
          "blender: promoted displayColor arrives as Cd in SOPs")

    lop_usd = tmp + "/lop_mat.usd"   # dedicated stage: usd_path was re-authored above
    usd_bridge.author_usd([("gen_001", _make_subtool("Lm", 0, 1, 1), tex_path)], lop_usd)
    lstage2 = Usd.Stage.Open(lop_usd)
    lop_texs = usd_bridge.Bridge()._stage_textures(lstage2)
    check(lop_texs == {"gen_001": tex_path},
          "send: texture extracted from LOP stage material: %s" % lop_texs)

    # Blender command protocol (redirected into the temp dir - no real cache touched)
    bb = usd_bridge.Bridge()
    bb.blender_dir = sdir + "/bl"
    bb.blender_cmd = bb.blender_dir + "/cmd.json"
    bb.blender_done = bb.blender_dir + "/done.json"
    cid = bb._write_blender_cmd("import_usd", {"usd": "X:/foo.usd"})
    with open(bb.blender_cmd) as fh:
        data = json.load(fh)
    check(data == {"usd": "X:/foo.usd", "op": "import_usd", "id": cid},
          "blender: cmd.json written with op/id/payload")
    with open(bb.blender_done + ".tmp", "w") as fh:
        json.dump({"id": cid, "ok": 1, "objects": 2}, fh)
    os.replace(bb.blender_done + ".tmp", bb.blender_done)
    res = bb._wait_blender_done(cid, timeout=3)
    check(bool(res) and res.get("ok") == 1 and res.get("objects") == 2,
          "blender: done.json waited for and parsed")
    cid2 = bb._write_blender_cmd("export_usd", {"usd": "X:/bar.usd"})
    check(not os.path.isfile(bb.blender_done),
          "blender: stale done.json cleared by next command")

    # generic app protocol serves maya paths too
    bm = usd_bridge.Bridge()
    bm.maya_dir = sdir + "/maya"
    mcid = bm._write_app_cmd("maya", "import_usd", {"usd": "X:/m.usd"})
    with open(bm.maya_dir + "/cmd.json") as fh:
        mdata = json.load(fh)
    check(mdata["op"] == "import_usd" and mdata["id"] == mcid,
          "maya: cmd protocol works via generic writer")

    # visibility: invisible prims pruned from an exported stage
    vis_usd = sdir + "/vis.usd"
    usd_bridge.author_usd([("Vis", _make_subtool("Va", 0, 1, 0), None),
                           ("Hid", _make_subtool("Vb", 3, 0, 1), None)], vis_usd)
    vstage = Usd.Stage.Open(vis_usd)
    UsdGeom.Imageable(vstage.GetPrimAtPath("/root/Hid")).MakeInvisible()
    vstage.GetRootLayer().Save()
    del vstage
    pruned = usd_bridge.Bridge._prune_invisible(vis_usd)
    vcheck = Usd.Stage.Open(vis_usd)
    hid = vcheck.GetPrimAtPath("/root/Hid")
    check(pruned == 1 and (not hid or not hid.IsActive()),
          "visibility: invisible prim pruned from export (pruned=%d)" % pruned)
    check(vcheck.GetPrimAtPath("/root/Vis").IsActive(), "visibility: visible prim kept")

    # visibility: _3d_hidden_primitives group dropped on SOP send
    hgeo_node = hou.node("/obj").createNode("geo", "hid_src")
    hbox = hgeo_node.createNode("box")
    hgrp = hbox.createOutputNode("groupcreate")
    hgrp.parm("groupname").set("_3d_hidden_primitives")
    hgrp.parm("grouptype").set("primitive")
    hgrp.parm("basegroup").set("0-2")  # hide 3 of 6 faces
    hg = hgrp.geometry().freeze()
    dropped = bridge._drop_hidden_prims(hg)
    check(len(dropped.iterPrims()) == 3,
          "visibility: hidden prim group dropped (%d left)" % len(dropped.iterPrims()))

    from PIL import Image
    fsrc, fdst = sdir + "/flipsrc.png", sdir + "/flipdst.png"
    im = Image.new("RGB", (1, 2))
    im.putpixel((0, 0), (255, 0, 0))   # top red
    im.putpixel((0, 1), (0, 255, 0))   # bottom green
    im.save(fsrc)
    bridge._copy_texture(fsrc, fdst, True)
    out = Image.open(fdst)
    check(out.getpixel((0, 0)) == (0, 255, 0) and out.getpixel((0, 1)) == (255, 0, 0),
          "send: _copy_texture flips vertically")

    g0 = hou.node("/obj").createNode("geo", "wind").createNode("box").geometry().freeze()
    n0 = g0.prims()[0].normal()
    n1 = bridge._reverse_winding(g0).prims()[0].normal()
    check(n0.dot(n1) < 0, "send: _reverse_winding flips face normals (dot=%.2f)" % n0.dot(n1))

    # Winding model: authored USD is true right-handed; unpackusd converts back to
    # native; ZBrush wants NATIVE winding -> only opposite-wound geometry is flipped.
    wusd2 = tmp + "/windsrc.usd"
    usd_bridge.author_usd([("W", g0, None)], wusd2)
    wgeo = hou.node("/obj").createNode("geo", "windu")
    wimp = wgeo.createNode("usdimport"); wimp.parm("filepath1").set(wusd2)
    wup = wimp.createOutputNode("unpackusd"); wup.parm("output").set(1)
    wg = wup.geometry().freeze()
    nu = wg.prims()[0].normal()
    check(n0.dot(nu) > 0.99, "wind: authored RH file unpacks to native (dot=%.2f)" % n0.dot(nu))
    check(not bridge._winding_opposite_native(g0), "wind: native box detected native")
    check(not bridge._winding_opposite_native(wg), "wind: unpacked box detected native")
    rev = bridge._reverse_winding(g0)
    check(bridge._winding_opposite_native(rev), "wind: reversed box detected opposite")
    pieces_n = bridge._split(g0, "n")
    nn = pieces_n[0][1].prims()[0].normal()
    check(n0.dot(nn) > 0.99, "wind: native piece sent as-is (dot=%.2f)" % n0.dot(nn))
    pieces_r = bridge._split(rev, "r")
    nr = pieces_r[0][1].prims()[0].normal()
    check(n0.dot(nr) > 0.99, "wind: opposite piece auto-corrected to native (dot=%.2f)" % n0.dot(nr))

    print()
    if _failures:
        print("RESULT: FAIL (%d)" % len(_failures))
        for f in _failures:
            print("   - " + f)
        sys.exit(1)
    print("RESULT: PASS")


if __name__ == "__main__":
    main()

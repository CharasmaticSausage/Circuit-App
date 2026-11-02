"""Offline unit checks (stdlib + KiCad's pcbnew; no server, no network). Run from the pcbstudio folder:

  python tests/test_core.py        -> prints each check and "ALL N CHECKS PASSED" (exit 1 on any failure)
"""
from __future__ import annotations

import os
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["PCBSTUDIO_DATA"] = tempfile.mkdtemp(prefix="pcbs_test_")  # never touch the real projects

from server import fab, jlc, netlist, phases, workspace  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "tiny_ldo_mcu.net"
CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


@check
def tokenizer_keeps_decimals():
    assert jlc._tokens("LDO 3.3V 500mA") == ["ldo", "3.3v", "500ma"], jlc._tokens("LDO 3.3V 500mA")
    row = {"mpn": "XC6206P332MR", "package": "SOT-23", "description": "3.3V 200mA LDO", "lcsc": "C5446"}
    assert jlc._matches(row, "3.3V")
    assert not jlc._matches({**row, "description": "13.3V LDO"}, "3.3V")
    assert not jlc._matches({**row, "description": "3.0V LDO"}, "3.3V")


def _part(lcsc, stock, price, basic=False, package="SOT-23"):
    return {"lcsc": lcsc, "mpn": lcsc, "package": package, "description": "", "stock": stock,
            "tiers": [(1, price)], "basic": basic, "preferred": False, "category": None}


@check
def ranker_order():
    parts = [_part("A", 5, 0.01), _part("B", 100000, 0.05), _part("C", None, 0.001),
             _part("D", 100000, 0.05, basic=True), _part("E", 100000, 0.01, package="SOT-89")]
    r = jlc.rank(parts, 2, 5, 3.0, "SOT-23")
    order = [p["lcsc"] for p in r]
    # enough stock first; package match next; then cost incl. the $3 Extended fee (D is Basic: no fee)
    assert order == ["D", "B", "E", "C", "A"], order
    assert r[0]["need"] == 10 and r[-1]["stock_state"] == "short"


@check
def phase_state_machine():
    p = workspace.create_project("unit-phases")
    pid = p["id"]
    s = phases.state(pid)
    assert [s["phases"][k]["status"] for k in phases.ORDER] == ["active", "locked", "locked"]
    try:
        phases.require(pid, "placement")
        raise AssertionError("placement must need components approved")
    except ValueError:
        pass
    phases.approve(pid, "components")
    phases.approve(pid, "placement")
    s = phases.reopen(pid, "components")
    assert [s["phases"][k]["status"] for k in phases.ORDER] == ["active", "stale", "stale"], s["phases"]


@check
def sexpr_parser():
    t = netlist.parse_sexpr('(a "b c" (d "e\\"f") g)')
    assert t == ["a", "b c", ["d", 'e"f'], "g"], t
    for bad in ("(a (b)", "(a))"):
        try:
            netlist.parse_sexpr(bad)
            raise AssertionError(f"accepted {bad!r}")
        except ValueError:
            pass


@check
def netlist_import_and_validation():
    nl = netlist.validate(netlist.from_kicad_net(FIX.read_text(encoding="utf-8"), "fixture"))
    assert nl["ok"] and len(nl["parts"]) == 12 and len(nl["nets"]) == 10, nl["issues"]
    assert {p["ref"]: p["lcsc"] for p in nl["parts"]}["U1"] == "C51118"
    bad = netlist.from_draft([{"ref": "U1", "footprint": "Package_TO_SOT_SMD:SOT-23-5"},
                              {"ref": "R1", "footprint": ""}],
                             [{"name": "X", "pins": ["U1.9", "R1.1"]}, {"name": "Y", "pins": ["U1.1"]}])
    v = netlist.validate(bad)
    msgs = " | ".join(i["msg"] for i in v["issues"])
    assert not v["ok"] and "no pad '9'" in msgs and "no footprint" in msgs and "only 1 pin" in msgs, msgs


@check
def reimport_follows_footprint_change():
    """Re-importing a netlist with a corrected footprint renames the netlist role instead of leaving it stale."""
    pid = workspace.create_project("unit-reimport")["id"]
    text = FIX.read_text(encoding="utf-8")
    netlist.save(pid, netlist.from_kicad_net(text, "a"))
    netlist.save(pid, netlist.from_kicad_net(text.replace("SOIC-8_5.3x5.3mm", "SOIC-8_3.9x4.9mm"), "b"))
    names = [r["name"] for r in phases.components(pid)["roles"] if r.get("refs") == ["U2"]]
    assert names == ["ATtiny85-20SU (SOIC-8_3.9x4.9mm_P1.27mm)"], names


@check
def symbol_pins_from_kicad_library():
    """The AP2112K pinout the fixture uses (1 VIN, 2 GND, 3 EN, 5 VOUT), read from KiCad's symbol, via `extends`."""
    r = netlist.symbol_pins("AP2112K-3.3")
    assert {k: v["name"] for k, v in r["pins"].items()} == {"1": "VIN", "2": "GND", "3": "EN", "4": "NC", "5": "VOUT"}, r
    assert r["via"] == ["AP2204K-1.5"], r["via"]
    try:
        netlist.symbol_pins("ATTINY85-20SU")
        raise AssertionError("unknown symbol accepted")
    except ValueError as e:
        assert "ATTINY85-20S" in str(e), e


@check
def home_summary_offline():
    """A home tile's data comes from project files and the JLC cache only: phase, counts, and an honest cost
    (chosen parts with no cached price are counted as unpriced, never guessed)."""
    from server import home
    pid = workspace.create_project("unit-home")["id"]
    netlist.save(pid, netlist.from_kicad_net(FIX.read_text(encoding="utf-8"), "fixture"))
    phases.save_components(pid, {**phases.components(pid), "requirements": "tiny test board"})
    s = home.summary(pid)
    assert s["phase"]["current"] == "components" and s["phase"]["status"] == "active", s["phase"]
    assert s["netlist"] == {"parts": 12, "nets": 10, "ok": True}, s["netlist"]
    assert s["scope"] == "tiny test board" and not s["has_board"], s
    c = s["cost"]  # fresh data folder: nothing cached, so every chosen part is unpriced
    assert c["parts_usd"] is None and c["unpriced"] == s["roles"]["chosen"] > 0, (c, s["roles"])
    assert jlc.cached_lookup("C51118") is None
    assert [x["id"] for x in home.summaries()].count(pid) == 1


@check
def filter_values_resolved():
    """jlcsearch matches filter values exactly ('female' finds nothing, 'Female' finds 100); guesses are mapped to
    the stored value or rejected with the valid list, never sent to silently match nothing."""
    html = ('<input type="text" name="search" /><input name="gender" list="g" /><input name="capacitance" list="c" />'
            '<input name="mounting_style" list="m" /><datalist id="g"><option value="Female"><option value="Male">'
            '<option value="male"><option value="-"></datalist><datalist id="c"><option value="1.0e-07"></datalist>'
            '<datalist id="m"><option value="Surface Mount"><option value="Surface Mount&#65292;Right Angle"></datalist>')
    form = jlc.parse_filter_form(html)
    assert form["gender"] == ["Female", "Male", "male"] and "search" not in form, form
    out, notes = jlc.resolve_filters({"gender": "female", "capacitance": "0.0000001"}, form)
    assert out == {"gender": "Female", "capacitance": "0.0000001"} and len(notes) == 1, (out, notes)
    assert jlc.resolve_filters({"gender": "male"}, form)[0] == {"gender": "male"}  # exact wins
    for bad, word in (({"mounting_style": "SMD"}, "Surface Mount"), ({"colour": "red"}, "gender"),
                      ({"mounting_style": "surface"}, "several")):
        try:
            jlc.resolve_filters(bad, form)
            raise AssertionError(f"accepted {bad}")
        except jlc.JlcError as e:
            assert word in str(e), e


@check
def query_drops_only_plain_unknown_words():
    """No part says 'receptacle': drop it and say so. A value word ('9.9v') is never dropped."""
    rows = [{"lcsc": 1, "mfr": "TYPE-C 6P", "description": "Female Type-C USB Connectors", "package": "SMD"},
            {"lcsc": 2, "mfr": "U262", "description": "Male USB-A USB Connectors", "package": "SMD"}]
    real = jlc._get
    jlc._get = lambda path, ttl=0: ({"components": rows}, 0.0)
    try:
        r = jlc.search("Type-C receptacle", subcategory="USB Connectors")
        assert [p["lcsc"] for p in r["parts"]] == ["C1"] and "ignored receptacle" in r["notes"][-1], r
        r = jlc.search("Type-C 9.9v", subcategory="USB Connectors")
        assert r["total"] == 0 and r["notes"][-1] == jlc.NOT_FOUND_NOTE and "9.9v" in r["notes"][-2], r["notes"]
    finally:
        jlc._get = real


@check
def part_role_cannot_overwrite_requirements():
    """Per-role specs go in `notes`; only set_board_requirements changes the board-wide text."""
    import asyncio
    from server import tools
    pid = workspace.create_project("unit-req")["id"]
    phases.save_components(pid, {**phases.components(pid), "requirements": "user's text"})
    schema = next(t for t in tools.TOOLS if t["name"] == "set_part_role")["input_schema"]
    assert tools.validate(schema, {"name": "USB-C", "requirements": "x"}), "set_part_role must reject requirements"

    async def emit(_):
        pass
    ctx = tools.ToolContext(pid, None, None, emit)
    ok, res = asyncio.run(tools.execute("set_board_requirements", {"requirements": "user's text + 5 V in", "boards": 10}, ctx))
    assert ok and phases.components(pid)["requirements"] == "user's text + 5 V in" and res["boards"] == 10, res


@check
def outline_shapes():
    from server import layout
    assert len(layout.outline_points("rect", 30, 20)) == 4
    oval = layout.outline_points("oval", 40, 20)
    xs, ys = [p[0] for p in oval], [p[1] for p in oval]
    assert abs(max(xs) - min(xs) - 40) < 1e-6 and abs(max(ys) - min(ys) - 20) < 0.05
    c = layout.outline_points("circle", 25, 0)
    assert abs(max(p[1] for p in c) - min(p[1] for p in c) - 25) < 0.05


@check
def fab_pro_has_rules():
    pro = fab.kicad_pro(Path("x.kicad_pcb"), fab.get(None), ["GND"])
    r = pro["board"]["design_settings"]["rules"]
    assert r["min_track_width"] == 0.127 and pro["net_settings"]["classes"][0]["track_width"] == 0.25
    assert pro["net_settings"]["netclass_patterns"] == [{"pattern": "GND", "netclass": "Power"}]


@check
def placer_invariants():
    """Build the fixture board and place it: no courtyard overlap beyond the spacing margin, every part
    inside the outline, deterministic for a seed."""
    import numpy as np
    import pcbnew
    from server import board as B, layout
    d = Path(tempfile.mkdtemp(prefix="pcbs_place_"))
    bp = d / "t.kicad_pcb"
    pcbnew.SaveBoard(str(bp), pcbnew.CreateEmptyBoard())
    doc = B.BoardDoc(str(bp), str(d / "snaps"))
    nl = netlist.validate(netlist.from_kicad_net(FIX.read_text(encoding="utf-8"), "fixture"))
    layout.build_board(doc, nl, "rect", 32, 24, 1, fab.DEFAULT)
    r = layout.place(doc, fab.DEFAULT, moves=8000, seed=3)
    assert r["after"]["wirelength_mm"] < r["before"]["wirelength_mm"], r
    pl = layout.Placer(doc.board, fab.get(None))
    pl.grow[:] = 0  # bare courtyards
    t = pl.terms()
    assert t["overlap_mm2"] < 0.01, t
    poly = pl.poly
    for i in range(pl.n):
        box = pl.abs_box(i)
        corners = np.array([[box[0], box[1]], [box[2], box[1]], [box[2], box[3]], [box[0], box[3]]])
        _, inside = layout._poly_dist(corners, poly)
        assert inside.all(), (pl.refs[i], box)
    assert (d / "t.kicad_pro").is_file()


@check
def silkscreen_after_placement():
    """Placing the fixture leaves (almost) no silk_* DRC warnings: the LCSC number is not printed on the silkscreen
    (it was: 76 silk warnings before) and references are moved clear of pads and other silk. Real KiCad DRC."""
    import asyncio
    import pcbnew
    from server import board as B, drc, layout
    d = Path(tempfile.mkdtemp(prefix="pcbs_silk_"))
    bp = d / "t.kicad_pcb"
    pcbnew.SaveBoard(str(bp), pcbnew.CreateEmptyBoard())
    doc = B.BoardDoc(str(bp), str(d / "snaps"))
    nl = netlist.validate(netlist.from_kicad_net(FIX.read_text(encoding="utf-8"), "fixture"))
    layout.build_board(doc, nl, "rect", 32, 24, 1, fab.DEFAULT)
    silk = (pcbnew.F_SilkS, pcbnew.B_SilkS)
    shown = [(f.GetReference(), fl.GetName()) for f in doc.board.GetFootprints() for fl in f.GetFields()
             if fl.IsVisible() and fl.GetLayer() in silk and fl.GetName() != "Reference"]
    assert not shown, shown
    # a board built before the fix: the pass hides the LCSC text
    doc.board.GetFootprints()[0].SetField("LCSC", "C1")
    doc.board.GetFootprints()[0].GetField("LCSC").SetLayer(pcbnew.F_SilkS)
    doc.board.GetFootprints()[0].GetField("LCSC").SetVisible(True)
    r = layout.place(doc, fab.DEFAULT, moves=8000, seed=3)["references"]
    assert r["lcsc_text_hidden"] == [doc.board.GetFootprints()[0].GetReference()] and r["moved"], r
    res = asyncio.run(drc.run_file(str(bp)))
    n = sum(v for k, v in res["by_type"].items() if k.startswith("silk"))
    assert n <= 3, res["by_type"]
    # the router's re-place rounds run the same pass (they call the Placer directly, not layout.place)
    from server import routing
    rr = routing._refine(str(bp), fab.DEFAULT, {}, {}, 2)
    assert "references" in rr and "unresolved" in rr["references"], rr.keys()


def main():
    failed = 0
    for fn in CHECKS:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}\n{traceback.format_exc()}")
    if failed:
        print(f"{failed} of {len(CHECKS)} CHECKS FAILED")
        return 1
    print(f"ALL {len(CHECKS)} CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())

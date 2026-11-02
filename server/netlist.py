"""Phase 1b: the netlist (which pin connects to which), for designs that start without a board.

Two ways in:
- import a KiCad schematic (.kicad_sch, exported through `kicad-cli sch export netlist`) or a KiCad
  netlist file (.net, the kicadsexpr format);
- the assistant drafts one from the chosen parts (`draft_netlist`), and the user checks every pin
  against the datasheet before approving Phase 1.

Either way the result is netlist.json in the project:
  {"source", "parts": [{ref, value, footprint "Lib:Name", lcsc, fields}], "nets": [{name, nodes: [[ref, pin]]}],
   "issues": [{level, msg}], "updated_at"}
`validate` checks footprints against the installed KiCad libraries and every pin against the
footprint's pad numbers. A valid pad number does not prove the pinout is right: that is the user's
check (the Components tab lists pin -> net per part for it).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from . import phases, workspace

LCSC_FIELDS = ("lcsc", "lcsc part", "lcsc part #", "jlcpcb part #", "jlc part", "jlcpcb", "lcsc#")


# ---- S-expressions ----------------------------------------------------------------------------
_TOK = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))', re.S)


def parse_sexpr(text: str):
    """Parse one S-expression into nested lists of strings (quoted and bare atoms alike)."""
    stack: list[list] = [[]]
    pos = 0
    n = len(text)
    while pos < n:
        m = _TOK.match(text, pos)
        if not m:
            if text[pos:].strip():
                raise ValueError(f"bad S-expression near offset {pos}: {text[pos:pos + 40]!r}")
            break
        pos = m.end()
        if m.group(1):
            stack.append([])
        elif m.group(2):
            if len(stack) < 2:
                raise ValueError(f"unbalanced ')' at offset {pos}")
            done = stack.pop()
            stack[-1].append(done)
        elif m.group(3) is not None:
            stack[-1].append(re.sub(r"\\(.)", r"\1", m.group(3)))
        else:
            stack[-1].append(m.group(4))
    if len(stack) != 1:
        raise ValueError("unbalanced '(' (file truncated?)")
    return stack[0][0] if len(stack[0]) == 1 else stack[0]


def _kids(node, tag):
    return [x for x in node[1:] if isinstance(x, list) and x and x[0] == tag]


def _kid(node, tag):
    k = _kids(node, tag)
    return k[0] if k else None


def _val(node, tag, default=""):
    k = _kid(node, tag)
    return k[1] if k and len(k) > 1 and isinstance(k[1], str) else default


def from_kicad_net(text: str, source: str) -> dict:
    """KiCad 'kicadsexpr' netlist -> our netlist dict."""
    root = parse_sexpr(text)
    if not (isinstance(root, list) and root and root[0] == "export"):
        raise ValueError("not a KiCad netlist (expected (export ...))")
    parts = []
    comps = _kid(root, "components") or ["components"]
    for c in _kids(comps, "comp"):
        fields = {}
        fl = _kid(c, "fields")
        for f in _kids(fl, "field") if fl else []:
            name = _val(f, "name")
            vals = [x for x in f[1:] if isinstance(x, str)]
            if name:
                fields[name] = vals[-1] if vals else ""
        for pr in _kids(c, "property"):  # KiCad 7+ also lists properties
            name, value = _val(pr, "name"), _val(pr, "value")
            if name and name not in fields:
                fields[name] = value
        lcsc = next((v for k, v in fields.items() if k.strip().lower() in LCSC_FIELDS and v.strip()), "")
        parts.append({"ref": _val(c, "ref"), "value": _val(c, "value"), "footprint": _val(c, "footprint"),
                      "lcsc": lcsc.strip().upper(), "fields": {k: v for k, v in fields.items() if v}})
    nets = []
    nl = _kid(root, "nets") or ["nets"]
    for net in _kids(nl, "net"):
        nodes = [[_val(nd, "ref"), _val(nd, "pin")] for nd in _kids(net, "node")]
        nets.append({"name": _val(net, "name"), "nodes": nodes})
    return {"source": source, "parts": parts, "nets": nets}


def export_schematic(sch: Path) -> str:
    cli = workspace.kicad_cli()
    if not cli:
        raise RuntimeError("kicad-cli not found: install KiCad or set its path in Settings")
    fd, out = tempfile.mkstemp(suffix=".net", prefix="pcbs_")
    os.close(fd)
    try:
        r = subprocess.run([cli, "sch", "export", "netlist", "--format", "kicadsexpr", "-o", out, str(sch)],
                           capture_output=True, text=True, timeout=180)
        text = Path(out).read_text(encoding="utf-8") if os.path.getsize(out) else ""
        if r.returncode != 0 or not text:
            raise RuntimeError(f"kicad-cli netlist export failed (exit {r.returncode}): {(r.stderr or r.stdout)[-500:]}")
        return text
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


# ---- footprint libraries ----------------------------------------------------------------------
_libs: dict[str, str] | None = None
_index: list[tuple[str, str]] | None = None  # (lib, footprint name)


def _kicad_share() -> Path | None:
    cli = workspace.kicad_cli()
    if cli:
        p = Path(cli).resolve().parent.parent / "share" / "kicad"
        if p.is_dir():
            return p
    return None


def _expand(uri: str) -> str:
    share = _kicad_share()

    def sub(m):
        k = m.group(1)
        if os.environ.get(k):
            return os.environ[k]
        if re.fullmatch(r"KICAD\d*_FOOTPRINT_DIR", k) and share:
            return str(share / "footprints")
        if re.fullmatch(r"KICAD\d*_3DMODEL_DIR", k) and share:
            return str(share / "3dmodels")
        return m.group(0)
    return re.sub(r"\$\{([A-Za-z0-9_]+)\}", sub, uri)


def _read_table(path: Path, out: dict, depth: int = 0) -> None:
    if depth > 4 or not path.is_file():
        return
    root = parse_sexpr(path.read_text(encoding="utf-8"))
    for lib in _kids(root, "lib"):
        name, kind, uri = _val(lib, "name"), _val(lib, "type"), _expand(_val(lib, "uri"))
        if kind.lower() == "table":  # KiCad 10 nests the stock table
            _read_table(Path(uri), out, depth + 1)
        elif name and name not in out:
            out[name] = uri


def libraries() -> dict[str, str]:
    """Footprint library nickname -> .pretty folder, from the user's fp-lib-table (KiCad's own table)."""
    global _libs
    if _libs is None:
        found: dict[str, str] = {}
        appdata = Path(os.environ.get("APPDATA", Path.home()))
        for ver in sorted((appdata / "kicad").glob("*/fp-lib-table"), reverse=True):
            _read_table(ver, found)
            if found:
                break
        share = _kicad_share()
        if not found and share and (share / "template" / "fp-lib-table").is_file():
            _read_table(share / "template" / "fp-lib-table", found)
        if not found and share:  # no table at all: every stock .pretty folder
            for d in (share / "footprints").glob("*.pretty"):
                found[d.stem] = str(d)
        _libs = found
    return _libs


def footprint_file(fpid: str) -> Path | None:
    if ":" not in (fpid or ""):
        return None
    lib, name = fpid.split(":", 1)
    d = libraries().get(lib)
    if not d:
        return None
    f = Path(d) / f"{name}.kicad_mod"
    return f if f.is_file() else None


def find_footprints(query: str, limit: int = 40) -> dict:
    """Search footprint names across the installed libraries. Words must all appear (case-insensitive)."""
    global _index
    if _index is None:
        idx = []
        for lib, d in libraries().items():
            try:
                idx += [(lib, f[:-10]) for f in os.listdir(d) if f.endswith(".kicad_mod")]
            except OSError:
                pass
        _index = idx
    words = [w for w in re.split(r"\s+", query.lower().strip()) if w]
    if not words:
        raise ValueError("give search words, e.g. 'SOT-23-5', 'USB_C Receptacle 16P', '0603 capacitor'")
    hits = [f"{lib}:{n}" for lib, n in _index if all(w in f"{lib}:{n}".lower() for w in words)]
    hits.sort(key=lambda s: (len(s), s))
    return {"query": query, "total": len(hits), "footprints": hits[:limit], "libraries": len(libraries())}


_PAD = re.compile(r'\(pad\s+"([^"]*)"\s+(\w+)\s+(\w+)')


def footprint_pads(fpid: str) -> dict:
    f = footprint_file(fpid)
    if f is None:
        raise ValueError(f"footprint {fpid!r} not found in the installed KiCad libraries (use find_footprint)")
    text = f.read_text(encoding="utf-8")
    pads: dict[str, dict] = {}
    for num, kind, shape in _PAD.findall(text):
        if num and num not in pads:
            pads[num] = {"type": kind, "shape": shape}
    descr = re.search(r'\(descr\s+"((?:[^"\\]|\\.)*)"', text)
    return {"footprint": fpid, "pads": sorted(pads, key=_natural), "pad_types": pads,
            "description": descr.group(1) if descr else ""}


_SYM_TOP = re.compile(r'^\t\(symbol "([^"]+)"', re.M)
_PIN = re.compile(r'\(pin\s+(\w+)\s+\w+.*?\(name\s+"((?:[^"\\]|\\.)*)".*?\(number\s+"([^"]*)"', re.S)
_sym_index: dict[str, Path] | None = None


def _symbol_index() -> dict[str, Path]:
    """Upper-case symbol name -> .kicad_sym file, over KiCad's installed symbol libraries."""
    global _sym_index
    if _sym_index is None:
        idx: dict[str, Path] = {}
        share = _kicad_share()
        for f in sorted((share / "symbols").glob("*.kicad_sym")) if share else []:
            try:
                for name in _SYM_TOP.findall(f.read_text(encoding="utf-8")):
                    idx.setdefault(name.upper(), f)
            except OSError:
                pass
        _sym_index = idx
    return _sym_index


def _symbol_block(text: str, name: str) -> str:
    i = text.find(f'\t(symbol "{name}"')
    j = text.find('\n\t(symbol "', i + 10)
    return text[i:j if j > 0 else len(text)]


def symbol_pins(name: str) -> dict:
    """Pin number -> pin name for a part, from KiCad's own symbol library (follows `extends`). This is the reference
    to check a netlist's pinout against; it is a library, not the datasheet, so the datasheet still wins."""
    idx = _symbol_index()
    key = (name or "").strip().upper()
    if key not in idx:
        near = sorted(k for k in idx if key and len(k) >= 4 and (key in k or k in key))[:15]
        raise ValueError(f"no KiCad symbol named {name!r}" + (f"; similar: {', '.join(near)}" if near else ""))
    f = idx[key]
    text = f.read_text(encoding="utf-8")
    real = next(n for n in _SYM_TOP.findall(text) if n.upper() == key)
    chain, blk = [real], _symbol_block(text, real)
    while (m := re.search(r'\(extends\s+"([^"]+)"\)', blk)) and len(chain) < 5:
        chain.append(m.group(1))
        blk = _symbol_block(text, m.group(1))
    pins = {}
    for kind, pname, num in _PIN.findall(blk):
        pins.setdefault(num, {"name": pname, "type": kind})
    fp = re.search(r'\(property\s+"Footprint"\s+"([^"]*)"', _symbol_block(text, real)) or re.search(r'\(property\s+"Footprint"\s+"([^"]*)"', blk)
    ds = re.search(r'\(property\s+"Datasheet"\s+"([^"]*)"', _symbol_block(text, real))
    return {"symbol": f"{f.stem}:{real}", "via": chain[1:], "pins": dict(sorted(pins.items(), key=lambda kv: _natural(kv[0]))),
            "footprint": fp.group(1) if fp else "", "datasheet": ds.group(1) if ds else "",
            "note": "From KiCad's symbol library. Check the datasheet when they disagree."}


def _natural(s: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


# ---- validation and storage -------------------------------------------------------------------
def validate(nl: dict) -> dict:
    issues: list[dict] = []
    pads_of: dict[str, set] = {}
    seen = set()
    for p in nl["parts"]:
        ref = p.get("ref", "")
        if not ref:
            issues.append({"level": "error", "msg": "a part has no reference"})
            continue
        if ref in seen:
            issues.append({"level": "error", "msg": f"{ref}: duplicate reference"})
        seen.add(ref)
        if not p.get("footprint"):
            issues.append({"level": "error", "msg": f"{ref}: no footprint assigned (not guessed; set one)"})
            continue
        try:
            pads_of[ref] = set(footprint_pads(p["footprint"])["pads"])
        except ValueError as e:
            issues.append({"level": "error", "msg": f"{ref}: {e}"})
    used: dict[str, set] = {r: set() for r in pads_of}
    names = set()
    for n in nl["nets"]:
        if not n.get("name"):
            issues.append({"level": "error", "msg": "a net has no name"})
        if n.get("name") in names:
            issues.append({"level": "error", "msg": f"net {n['name']}: listed twice"})
        names.add(n.get("name"))
        if len(n["nodes"]) < 2:
            issues.append({"level": "warning", "msg": f"net {n['name']}: only {len(n['nodes'])} pin (nothing to route)"})
        for ref, pin in n["nodes"]:
            if ref not in seen:
                issues.append({"level": "error", "msg": f"net {n['name']}: unknown part {ref}"})
            elif ref in pads_of:
                if pin not in pads_of[ref]:
                    issues.append({"level": "error", "msg": f"net {n['name']}: {ref} has no pad {pin!r} "
                                                            f"(pads: {', '.join(sorted(pads_of[ref], key=_natural)[:12])}…)"})
                elif pin in used[ref]:
                    issues.append({"level": "error", "msg": f"{ref}.{pin} is on more than one net"})
                used[ref].add(pin)
    unconnected = {r: sorted(pads_of[r] - used[r], key=_natural) for r in pads_of if pads_of[r] - used[r]}
    for r, pins in unconnected.items():
        if not used[r]:
            issues.append({"level": "warning", "msg": f"{r}: no pin is connected to anything"})
    nl["issues"] = issues
    nl["unconnected_pads"] = unconnected
    nl["ok"] = not any(i["level"] == "error" for i in issues)
    return nl


def load(pid: str) -> dict | None:
    f = workspace.project_dir(pid) / "netlist.json"
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save(pid: str, nl: dict, link_roles: bool = True) -> dict:
    nl = validate(nl)
    nl["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    nl["approved"] = False
    phases._write(pid, "netlist.json", nl)
    if link_roles:
        _link_roles(pid, nl)
    if phases.state(pid)["phases"]["components"]["status"] == "approved":
        phases.reopen(pid, "components")
    return nl


def _link_roles(pid: str, nl: dict) -> None:
    """Make sure every part group (same value + footprint) is a Phase 1 role, reusing roles by refs."""
    groups: dict[tuple, list] = {}
    for p in nl["parts"]:
        groups.setdefault((p.get("value", ""), p.get("footprint", "")), []).append(p)
    data = phases.components(pid)
    for (value, fpid), ps in groups.items():
        refs = sorted((p["ref"] for p in ps), key=_natural)
        name = f"{value} ({fpid.split(':')[-1]})" if value else fpid
        same = next((r for r in data["roles"] if r.get("refs") == refs and r.get("notes") == "from the netlist"), None)
        if same:  # a re-import: keep the role (and its search), but follow a changed value or footprint
            if same["name"] != name:
                phases.upsert_role(pid, {"id": same["id"], "name": name})
                data = phases.components(pid)
            continue
        if any(set(refs) & set(r.get("refs") or []) for r in data["roles"]):
            continue  # already covered (e.g. the role the part was drafted from)
        lcsc = next((p["lcsc"] for p in ps if p.get("lcsc")), "")
        role = {"name": name, "qty": len(refs), "refs": refs, "package": "", "notes": "from the netlist"}
        if lcsc:
            role["chosen"] = lcsc  # the schematic's LCSC field; "Re-search" ranks it against alternatives
            role["query"] = lcsc
        phases.upsert_role(pid, role)
        data = phases.components(pid)


def summary(nl: dict | None) -> dict | None:
    """Compact view for the model and the UI: counts, issues, and pin -> net per part."""
    if not nl:
        return None
    pins: dict[str, dict] = {}
    for n in nl["nets"]:
        for ref, pin in n["nodes"]:
            pins.setdefault(ref, {})[pin] = n["name"]
    return {"source": nl.get("source"), "ok": nl.get("ok"), "approved": nl.get("approved", False),
            "updated_at": nl.get("updated_at"), "parts": len(nl["parts"]), "nets": len(nl["nets"]),
            "issues": nl.get("issues", []),
            "by_part": [{"ref": p["ref"], "value": p.get("value", ""), "footprint": p.get("footprint", ""),
                         "lcsc": p.get("lcsc", ""),
                         "pins": dict(sorted(pins.get(p["ref"], {}).items(), key=lambda kv: _natural(kv[0]))),
                         "unconnected": nl.get("unconnected_pads", {}).get(p["ref"], [])}
                        for p in sorted(nl["parts"], key=lambda p: _natural(p.get("ref", "")))]}


def from_draft(parts: list, nets: list, source: str = "draft") -> dict:
    """The assistant's draft: parts [{ref, value, footprint, lcsc}] and nets [{name, pins: ["U1.3", ...]}]."""
    out_parts = [{"ref": str(p["ref"]).strip(), "value": str(p.get("value", "")).strip(),
                  "footprint": str(p.get("footprint", "")).strip(), "lcsc": str(p.get("lcsc", "")).strip().upper(),
                  "fields": {}} for p in parts]
    out_nets = []
    for n in nets:
        nodes = []
        for pin in n.get("pins", []):
            if "." not in str(pin):
                raise ValueError(f"net {n.get('name')}: pin {pin!r} must look like REF.PAD (e.g. U1.3)")
            ref, pad = str(pin).split(".", 1)
            nodes.append([ref.strip(), pad.strip()])
        out_nets.append({"name": str(n.get("name", "")).strip(), "nodes": nodes})
    return {"source": source, "parts": out_parts, "nets": out_nets}


def import_file(pid: str, path: str) -> dict:
    p = Path(path).expanduser()
    if not p.is_file():
        raise ValueError(f"no such file: {path}")
    if p.suffix.lower() == ".kicad_sch":
        text = export_schematic(p)
    elif p.suffix.lower() == ".net":
        text = p.read_text(encoding="utf-8")
    else:
        raise ValueError("pick a KiCad schematic (.kicad_sch) or netlist (.net)")
    return save(pid, from_kicad_net(text, f"kicad:{p.name}"))

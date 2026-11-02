"""Projects, settings and paths.

Boards are never edited where they live. "Import" copies the board (plus the KiCad
project and custom-rules files beside it) into a project folder under the app data
directory, and every edit and every assistant write goes to that copy only.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path

DATA = Path(os.environ.get("PCBSTUDIO_DATA") or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "pcbstudio")
PROJECTS = DATA / "projects"
SETTINGS = DATA / "settings.json"

SIDECARS = (".kicad_pro", ".kicad_dru", ".kicad_prl")

DEFAULT_SETTINGS = {
    "provider": "",  # "" = Claude Code (subscription) when the CLI is installed, else the Anthropic API
    "model": "claude-opus-5-5",  # Anthropic API model
    "cc_model": "",  # Claude Code model alias; "" = the plan's default
    "openai_model": "",
    "effort": "high",
    "anthropic_api_key": "",
    "claude_cli": "",
    "openai_base_url": "http://localhost:11434/v1",
    "openai_api_key": "",
    "kicad_cli": "",
    "fastroute_exe": "",  # external autorouter (GPL-3.0, run as a separate program, never bundled)
    "route_rounds": "3",  # Phase 3: route -> DRC -> re-place rounds per run
    "route_max_time": "60",  # seconds per routing round
    "changelist_path": "",  # "" = docs/CHANGELIST.md in the PCB Studio folder
    "chat_side": "right",  # "right" (VS Code style, default) or "left"
    "jlc_extended_fee": "3",  # USD per unique Extended part (JLCPCB Economic PCBA, FAQ updated 2026-09-09)
    "browse_start": str(Path.home()),
}


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    try:
        s.update(json.loads(SETTINGS.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return s


def save_settings(patch: dict) -> dict:
    s = load_settings()
    for k, v in patch.items():
        if k in DEFAULT_SETTINGS:
            s[k] = v
    DATA.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(s, indent=2), encoding="utf-8")
    return s


def public_settings() -> dict:
    """Settings safe to send to the browser: the key is reduced to a has-key flag."""
    s = load_settings()
    key = s.pop("anthropic_api_key", "")
    s["anthropic_key_set"] = bool(key)
    s["anthropic_key_env"] = bool(os.environ.get("ANTHROPIC_API_KEY"))
    s["openai_key_set"] = bool(s.pop("openai_api_key", ""))
    return s


def kicad_cli() -> str | None:
    s = load_settings().get("kicad_cli")
    if s and Path(s).is_file():
        return s
    found = shutil.which("kicad-cli")
    if found:
        return found
    for base in (Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "KiCad",):
        if base.is_dir():
            for ver in sorted(base.iterdir(), reverse=True):
                exe = ver / "bin" / "kicad-cli.exe"
                if exe.is_file():
                    return str(exe)
    return None


# ---- projects --------------------------------------------------------------------------
def _slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-.") or "project"
    return s[:60]


def project_dir(pid: str) -> Path:
    """Resolve a project id to its folder, refusing anything that escapes PROJECTS."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", pid or ""):
        raise ValueError("bad project id")
    p = (PROJECTS / pid).resolve()
    if p.parent != PROJECTS.resolve() or not p.is_dir():
        raise ValueError(f"no project {pid!r}")
    return p


def meta(pid: str) -> dict:
    return json.loads((project_dir(pid) / "project.json").read_text(encoding="utf-8"))


def board_path(pid: str) -> str:
    d = project_dir(pid)
    return str(d / meta(pid)["board"])


def list_projects() -> list[dict]:
    out = []
    if PROJECTS.is_dir():
        for d in PROJECTS.iterdir():
            mf = d / "project.json"
            if mf.is_file():
                try:
                    m = json.loads(mf.read_text(encoding="utf-8"))
                    out.append({"id": d.name, **m})
                except ValueError:
                    pass
    out.sort(key=lambda m: m.get("imported_at", ""), reverse=True)
    return out


def import_board(src: str, name: str | None = None) -> dict:
    srcp = Path(src).expanduser().resolve()
    if srcp.suffix.lower() != ".kicad_pcb" or not srcp.is_file():
        raise ValueError("pick an existing .kicad_pcb file")
    base = _slug(name or srcp.stem)
    pid, k = base, 2
    while (PROJECTS / pid).exists():
        pid, k = f"{base}-{k}", k + 1
    d = PROJECTS / pid
    d.mkdir(parents=True)
    shutil.copy2(srcp, d / srcp.name)
    copied, missing = [srcp.name], []
    for ext in SIDECARS:
        side = srcp.with_suffix(ext)
        if side.is_file():
            shutil.copy2(side, d / side.name)
            copied.append(side.name)
        elif ext != ".kicad_prl":
            missing.append(side.name)
    m = {"name": name or srcp.stem, "board": srcp.name, "source": str(srcp),
         "imported_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "copied": copied, "missing": missing}
    (d / "project.json").write_text(json.dumps(m, indent=2), encoding="utf-8")
    return {"id": pid, **m}


def create_project(name: str) -> dict:
    """A new design with no board yet (Phase 1 starts from requirements; the board is built in Phase 2).
    The caller saves an empty board to the returned path on the pcbnew thread."""
    if not (name or "").strip():
        raise ValueError("give the new design a name")
    base = _slug(name)
    pid, k = base, 2
    while (PROJECTS / pid).exists():
        pid, k = f"{base}-{k}", k + 1
    d = PROJECTS / pid
    d.mkdir(parents=True)
    m = {"name": name.strip(), "board": f"{base}.kicad_pcb", "source": "", "new_design": True,
         "imported_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "copied": [], "missing": []}
    (d / "project.json").write_text(json.dumps(m, indent=2), encoding="utf-8")
    return {"id": pid, **m, "board_path": str(d / m["board"])}


def browse(path: str | None, exts: tuple = (".kicad_pcb",)) -> dict:
    """List folders and KiCad boards in one directory, for the import dialog."""
    p = Path(path or load_settings()["browse_start"]).expanduser()
    if not p.is_dir():
        p = Path.home()
    p = p.resolve()
    dirs, boards = [], []
    try:
        for e in sorted(p.iterdir(), key=lambda e: e.name.lower()):
            if e.name.startswith("."):
                continue
            if e.is_dir():
                dirs.append(e.name)
            elif e.suffix.lower() in exts:
                boards.append({"name": e.name, "size": e.stat().st_size})
    except PermissionError:
        pass
    parent = str(p.parent) if p.parent != p else None
    return {"path": str(p), "parent": parent, "dirs": dirs, "boards": boards}


# ---- chat transcripts ------------------------------------------------------------------
def chat_file(pid: str, cid: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", cid or ""):
        raise ValueError("bad conversation id")
    d = project_dir(pid) / "chats"
    d.mkdir(exist_ok=True)
    return d / f"{cid}.json"


def load_chat(pid: str, cid: str) -> dict:
    f = chat_file(pid, cid)
    if f.is_file():
        return json.loads(f.read_text(encoding="utf-8"))
    return {"id": cid, "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "provider": "anthropic", "native": [], "ui": []}


def save_chat(pid: str, chat: dict) -> None:
    f = chat_file(pid, chat["id"])
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(chat), encoding="utf-8")
    os.replace(tmp, f)


def list_chats(pid: str) -> list[dict]:
    d = project_dir(pid) / "chats"
    out = []
    if d.is_dir():
        for f in d.glob("*.json"):
            try:
                c = json.loads(f.read_text(encoding="utf-8"))
            except ValueError:
                continue
            first = next((m["text"] for m in c.get("ui", []) if m.get("role") == "user"), "")
            out.append({"id": c["id"], "created": c.get("created", ""), "title": first[:80]})
    out.sort(key=lambda c: c["created"], reverse=True)
    return out

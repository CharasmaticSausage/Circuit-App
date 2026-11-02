"""The improvement changelist: flaws and ideas the in-app assistant records while it works.

The assistant calls `log_improvement` whenever a tool fails or misleads it, data is missing or wrong, a
workflow is clumsy, or the user wishes for something. Entries go to docs/CHANGELIST.md in the PCB Studio
folder (or the `changelist_path` setting), as open checkboxes for whoever develops the app next.
Same-title entries are not repeated; a repeat bumps the "seen" count instead.
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path

from . import workspace

DEFAULT = Path(__file__).resolve().parent.parent / "docs" / "CHANGELIST.md"
AREAS = ["components", "placement", "routing", "drc", "chat", "tools", "ui", "data", "other"]
KINDS = ["bug", "flaw", "improvement"]
_HEAD = """# PCB Studio changelist

Flaws and improvements recorded by the in-app assistant while it works (tool `log_improvement`), plus any
added by hand. Newest at the top. Tick an item when it is fixed and name the fix (file, commit).

"""
_lock = threading.Lock()


def path() -> Path:
    p = (workspace.load_settings().get("changelist_path") or "").strip()
    return Path(p) if p else DEFAULT


def _read() -> str:
    try:
        return path().read_text(encoding="utf-8")
    except OSError:
        return _HEAD


def log(title: str, detail: str, area: str = "other", kind: str = "improvement", evidence: str = "",
        project: str = "", provider: str = "") -> dict:
    title = " ".join(title.split())[:120]
    if not title:
        raise ValueError("title is required")
    with _lock:
        s = _read()
        m = re.search(rf"^- \[ \] \*\*{re.escape(title)}\*\*.*?\(seen (\d+)×\)", s, re.M)
        if m:  # already listed and still open: count it again rather than repeat it
            n = int(m.group(1)) + 1
            s = s[:m.start(1)] + str(n) + s[m.end(1):]
            path().parent.mkdir(parents=True, exist_ok=True)
            path().write_text(s, encoding="utf-8")
            return {"logged": False, "duplicate": True, "seen": n, "file": str(path())}
        lines = [f"- [ ] **{title}** · {kind} · {area} · {time.strftime('%Y-%m-%d %H:%M')}"
                 f"{f' · project {project}' if project else ''}{f' · {provider}' if provider else ''} (seen 1×)",
                 f"  {' '.join(detail.split())}"]
        if evidence.strip():
            lines.append(f"  Evidence: {' '.join(evidence.split())[:600]}")
        entry = "\n".join(lines) + "\n"
        body = s[len(_HEAD):] if s.startswith(_HEAD) else s
        path().parent.mkdir(parents=True, exist_ok=True)
        path().write_text(_HEAD + entry + body, encoding="utf-8")
    return {"logged": True, "file": str(path())}


def items() -> dict:
    s = _read()
    open_ = re.findall(r"^- \[ \] \*\*(.+?)\*\* · (\w+) · (\w+)", s, re.M)
    done = len(re.findall(r"^- \[x\] ", s, re.M | re.I))
    return {"file": str(path()), "open": [{"title": t, "kind": k, "area": a} for t, k, a in open_], "done": done,
            "text": s}

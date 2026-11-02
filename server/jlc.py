"""JLCPCB parts data: search, lookup, local cache, and the deterministic candidate ranker.

Source: the public jlcsearch API (https://jlcsearch.tscircuit.com), a daily mirror of JLCPCB's
parts catalogue built by the jlcparts project. It is NOT JLCPCB itself: stock and prices are as of
that snapshot and must be confirmed at order time. Standard library only.

Responses are cached in sqlite (DATA/jlc_cache.sqlite). Stock and prices go stale quickly, so cached
entries expire after CACHE_TTL_S; the category list is kept for a week.
"""
from __future__ import annotations

import json
import math
import re
from html import unescape
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import workspace

BASE = "https://jlcsearch.tscircuit.com"
SOURCE = "jlcsearch (daily mirror of the JLCPCB parts catalogue, via jlcparts)"
CACHE_TTL_S = 24 * 3600
_UA = "PCBStudio/0.3 (local open-source PCB tool)"
_MIN_GAP_S = 0.35  # be polite to a free service
_lock = threading.Lock()
_last = [0.0]
# Fallback when the category list can't be fetched (the endpoints that matter most for major parts).
_CATEGORIES = ["resistors", "capacitors", "ldos", "voltage_regulators", "boost_converters", "buck_boost_converters",
               "microcontrollers", "arm_processors", "risc_v_processors", "ble_chips", "ble_modules", "wifi_modules",
               "mosfets", "diodes", "leds", "switches", "headers", "jst_connectors", "fpc_connectors",
               "usb_c_connectors", "accelerometers", "gyroscopes", "adcs", "dacs", "io_expanders", "led_drivers",
               "fuses", "relays", "potentiometers", "microphones", "oled_display"]


class JlcError(RuntimeError):
    pass


# ---- HTTP + cache ---------------------------------------------------------------------------
def _db() -> sqlite3.Connection:
    workspace.DATA.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(workspace.DATA / "jlc_cache.sqlite", timeout=10)
    c.execute("CREATE TABLE IF NOT EXISTS http (url TEXT PRIMARY KEY, fetched REAL, body TEXT)")
    return c


def _get(path: str, ttl: float = CACHE_TTL_S) -> tuple[dict, float]:
    """GET a jlcsearch path as JSON. Returns (data, fetched_at). Blocking: call via asyncio.to_thread."""
    url = BASE + path
    with _db() as c:
        row = c.execute("SELECT fetched, body FROM http WHERE url = ?", (url,)).fetchone()
    if row and time.time() - row[0] < ttl:
        return json.loads(row[1]), row[0]
    with _lock:
        wait = _MIN_GAP_S - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8")
        data = json.loads(body)
    except (urllib.error.URLError, OSError, ValueError) as e:
        if row:  # offline: serve the stale copy rather than nothing
            return json.loads(row[1]), row[0]
        raise JlcError(f"JLC parts search is unreachable ({e}). Check the internet connection.") from e
    now = time.time()
    with _db() as c:
        c.execute("INSERT OR REPLACE INTO http VALUES (?, ?, ?)", (url, now, body))
    return data, now


def _rows(data: dict) -> list[dict]:
    for v in data.values():
        if isinstance(v, list):
            return v
    return []


# ---- normalisation ---------------------------------------------------------------------------
def _tiers(r: dict) -> list[tuple[int, float]]:
    """Price breaks as [(min_qty, unit_price_usd)]. jlcsearch gives a tier string, `price` or `price1`."""
    p = r.get("price")
    if isinstance(p, str) and ":" in p:
        out = []
        for part in p.split(","):
            m = re.fullmatch(r"\s*(\d+)-(\d*)\s*:\s*([\d.]+)\s*", part)
            if m:
                out.append((int(m.group(1)), float(m.group(3))))
        if out:
            return sorted(out)
    for k in ("price", "price1"):
        v = r.get(k)
        if isinstance(v, (int, float)):
            return [(1, float(v))]
        if isinstance(v, str):
            try:
                return [(1, float(v))]
            except ValueError:
                pass
    return []


def unit_price(tiers: list[tuple[int, float]], qty: int) -> float | None:
    best = None
    for q, p in tiers:
        if qty >= q:
            best = p
    return best if best is not None else (tiers[0][1] if tiers else None)


def normalize(r: dict) -> dict:
    lcsc = r.get("lcsc")
    stock = r.get("stock")
    if stock is None and r.get("in_stock") is False:
        stock = 0  # category lists give stock: null with in_stock: false for parts JLC has none of
    return {
        "lcsc": f"C{lcsc}" if lcsc is not None else None,
        "mpn": r.get("mfr"),
        "package": r.get("package"),
        "description": r.get("description") or "",
        "stock": stock if isinstance(stock, int) else None,  # None = unknown, not zero
        "tiers": _tiers(r),
        "basic": bool(r.get("is_basic") or r.get("basic")),
        "preferred": bool(r.get("is_preferred") or r.get("preferred")),
        "category": r.get("subcategory") or r.get("category"),  # JLC subcategory name when known
    }


def _tokens(s: str) -> list[str]:
    # Keep decimals whole: "3.3V" is one token, not "3" + "3v".
    return [t for t in re.findall(r"[a-z0-9]+(?:\.[0-9]+[a-z]*)?", s.lower()) if len(t) > 1]


def _matches(row: dict, q: str) -> bool:
    """Every query word must appear in the part's MPN, package or description. Values that start with a
    digit ("3.3v", "100nf") must stand alone, so 3.3v does not match 13.3v and 100nf does not match 1100nf."""
    hay = " ".join(str(row.get(k) or "") for k in ("mpn", "package", "description", "lcsc")).lower()
    hay_compact = re.sub(r"[^a-z0-9]", "", hay)
    for t in _tokens(q):
        if t[0].isdigit():
            if not re.search(rf"(?<![0-9.]){re.escape(t)}(?![0-9])", hay):
                return False
        elif t not in hay and t not in hay_compact:
            return False
    return True


# ---- public API (blocking) ---------------------------------------------------------------------
def categories() -> list[str]:
    try:
        req = urllib.request.Request(BASE + "/", headers={"User-Agent": _UA})
        with _db() as c:
            row = c.execute("SELECT fetched, body FROM http WHERE url = ?", (BASE + "/#cats",)).fetchone()
        if row and time.time() - row[0] < 7 * 86400:
            return json.loads(row[1])
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read().decode("utf-8", errors="replace")
        cats = sorted(set(re.findall(r'href="/([a-z0-9_]+)/list', html)) - {"categories", "footprint_index"})
        if cats:
            with _db() as c:
                c.execute("INSERT OR REPLACE INTO http VALUES (?, ?, ?)", (BASE + "/#cats", time.time(), json.dumps(cats)))
            return cats
    except (urllib.error.URLError, OSError):
        pass
    return _CATEGORIES


def parse_filter_form(html: str) -> dict[str, list[str] | None]:
    """Filter name -> the exact values the category's search form suggests (its <datalist>), or None for a
    free-form field (numbers in SI units). The service matches values exactly and case-sensitively."""
    lists = {m.group(1): [unescape(v) for v in re.findall(r'<option value="([^"]*)"', m.group(2))]
             for m in re.finditer(r'<datalist id="([^"]+)"[^>]*>(.*?)</datalist>', html, re.S)}
    out: dict[str, list[str] | None] = {}
    for tag in re.findall(r"<(?:input|select)\b[^>]*>", html):
        name = re.search(r'\bname="([a-z0-9_]+)"', tag)
        if not name or name.group(1) == "search":
            continue
        lid = re.search(r'\blist="([^"]+)"', tag)
        vals = lists.get(lid.group(1)) if lid else None
        out[name.group(1)] = [v for v in vals if v.strip() not in ("", "-")] if vals is not None else None
    return out


def filters(category: str) -> dict[str, list[str] | None]:
    """The parametric filters a category endpoint accepts, with their valid values (read from its search form)."""
    if not re.fullmatch(r"[a-z0-9_]+", category or ""):
        raise JlcError("category must be an endpoint name like 'ldos'")
    key = f"{BASE}/{category}/list#filters2"
    with _db() as c:
        row = c.execute("SELECT fetched, body FROM http WHERE url = ?", (key,)).fetchone()
    if row and time.time() - row[0] < 7 * 86400:
        return json.loads(row[1])
    try:
        req = urllib.request.Request(f"{BASE}/{category}/list", headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError) as e:
        raise JlcError(f"could not read the {category} filters ({e})") from e
    form = parse_filter_form(html)
    with _db() as c:
        c.execute("INSERT OR REPLACE INTO http VALUES (?, ?, ?)", (key, time.time(), json.dumps(form)))
    return form


def _numeric(vals: list[str]) -> bool:
    try:
        [float(x) for x in vals]
        return True
    except ValueError:
        return False


def filter_summary(form: dict[str, list[str] | None], cap: int = 40) -> dict:
    """The filter form as a tool shows it: text filters with their values (capped), numbers as 'number'."""
    out = {}
    for k, vals in form.items():
        if k in ("package", "is_basic", "is_preferred"):
            continue  # separate tool arguments
        if not vals or _numeric(vals):
            out[k] = "number in SI units (e.g. 0.0000001 for 100 nF)" if vals else "free-form"
        else:
            out[k] = vals[:cap] + ([f"... {len(vals) - cap} more"] if len(vals) > cap else [])
    return out


def resolve_filters(given: dict, form: dict[str, list[str] | None]) -> tuple[dict, list[str]]:
    """Map guessed filter values onto the exact strings the service stores ("female" -> "Female").
    An unknown name or value raises with the valid choices instead of silently matching nothing."""
    out, notes = {}, []
    for k, v in given.items():
        if v in (None, ""):
            continue
        if form and k not in form:
            raise JlcError(f"unknown filter {k!r}; this table has: {', '.join(sorted(form))}")
        vals = form.get(k) if form else None
        if not vals or str(v) in vals or _numeric(vals):  # numbers (SI units) are compared by the service
            out[k] = v
            continue
        s = str(v).strip().lower()
        hit = [x for x in vals if x.lower() == s] or [x for x in vals if s in x.lower()]
        if len(hit) == 1 or (hit and hit[0].lower() == s):
            out[k] = hit[0]
            notes.append(f"filter {k}={v!r} read as {hit[0]!r} (exact value the service stores)")
        else:
            raise JlcError(f"{k}={v!r} is not a value the service stores"
                           + (f"; it matches several: {', '.join(hit[:10])}" if hit else "")
                           + f". Valid values: {', '.join(vals[:25])}" + (" ..." if len(vals) > 25 else ""))
    return out, notes


def lookup(lcsc: str) -> dict | None:
    """One part by LCSC number (C1525 or 1525)."""
    m = re.fullmatch(r"[Cc]?(\d+)", (lcsc or "").strip())
    if not m:
        raise JlcError(f"{lcsc!r} is not an LCSC part number (like C1525)")
    # /components/list.json carries live-ish stock and every price break; /api/search often has stock: null.
    for path in (f"/components/list.json?search=C{m.group(1)}", f"/api/search?q=C{m.group(1)}&limit=3"):
        data, at = _get(path)
        for r in _rows(data):
            if str(r.get("lcsc")) == m.group(1):
                return {**normalize(r), "as_of": at}
    return None


def cached_lookup(lcsc: str) -> dict | None:
    """Like `lookup`, but from the local cache only (any age, never the network). None when not cached."""
    m = re.fullmatch(r"[Cc]?(\d+)", (lcsc or "").strip())
    if not m:
        return None
    with _db() as c:
        for path in (f"/components/list.json?search=C{m.group(1)}", f"/api/search?q=C{m.group(1)}&limit=3"):
            row = c.execute("SELECT fetched, body FROM http WHERE url = ?", (BASE + path,)).fetchone()
            if row:
                for r in _rows(json.loads(row[1])):
                    if str(r.get("lcsc")) == m.group(1):
                        return {**normalize(r), "as_of": row[0]}
    return None


def enrich(parts: list[dict]) -> list[dict]:
    """Fill stock and price breaks from the per-part lookup (category lists often lack both).
    One cached request per part, so call it only on the shortlist."""
    out = []
    for p in parts:
        if p.get("lcsc") and (p["stock"] is None or len(p["tiers"]) <= 1):
            try:
                full = lookup(p["lcsc"])
            except JlcError:
                full = None
            if full:
                p = {**p, "stock": full["stock"] if full["stock"] is not None else p["stock"],
                     "tiers": full["tiers"] or p["tiers"], "category": p["category"] or full["category"]}
        out.append(p)
    return out


_filter_form = filters

NOT_FOUND_NOTE = ("0 results does NOT mean JLC lacks the part: this mirror has no part-number (MPN) index and lists "
                  "at most the 100 best-stocked parts per query, so low- or unknown-stock parts (e.g. ATTINY85-20SU, "
                  "C89852) never appear. Say 'not found by search', and if the LCSC number is known (the "
                  "schematic's LCSC field, the datasheet, the user) check it with jlc_part.")


def search(q: str = "", category: str | None = None, filters: dict | None = None, package: str | None = None,
           basic_only: bool = False, limit: int = 40, subcategory: str | None = None) -> dict:
    """Find parts. Three routes, best first:
    subcategory  JLC's own subcategory name (e.g. "Voltage Regulators - Linear, Low Drop Out (LDO) Regulators"):
                 the 100 best-stocked parts in it (and the package), with stock and every price break.
    category     a jlcsearch parametric table ('capacitors', 'ldos'...) with numeric `filters`. These tables are
                 derived subsets and can miss common parts (the 'ldos' table has no ME6211).
    q only       free text. The service's own text index is unreliable, so this lists the 100 best-stocked
                 parts in the package and keeps those containing every word.
    `q` words always narrow the result client-side (MPN, package, description)."""
    notes = []
    if subcategory:
        params = {"subcategory_name": subcategory}
        if package:
            params["package"] = package
        if basic_only:
            params["is_basic"] = "true"
        data, at = _get("/components/list.json?" + urllib.parse.urlencode(params))
        rows = [normalize(r) for r in _rows(data)]
        if not rows:
            notes.append(f"No parts in subcategory {subcategory!r}" + (f" with package {package}" if package else "")
                         + ". Subcategory names must match JLC's exactly; jlc_part on a known part shows its name.")
        elif len(rows) >= 100:
            notes.append("The service returns at most the 100 best-stocked parts here; add a package to narrow it.")
    elif category:
        if not re.fullmatch(r"[a-z0-9_]+", category):
            raise JlcError("category must be an endpoint name like 'ldos' or 'capacitors'")
        params = {k: v for k, v in (filters or {}).items() if v not in (None, "")}
        if params:
            try:
                form = _filter_form(category)
            except JlcError:
                form = {}  # offline: send the values as given
            params, fnotes = resolve_filters(params, form)
            notes += fnotes
        if package:
            params["package"] = package
        data, at = _get(f"/{category}/list.json?" + urllib.parse.urlencode(params))
        rows = [normalize(r) for r in _rows(data)]
    else:
        if not package:
            raise JlcError("free-text search needs a package (or use a subcategory or category)")
        params = {"package": package}
        if basic_only:
            params["is_basic"] = "true"
        data, at = _get("/components/list.json?" + urllib.parse.urlencode(params))
        rows = [normalize(r) for r in _rows(data)]
    if q:
        before, pool = len(rows), rows
        rows = [r for r in rows if _matches(r, q)]
        if before and not rows:
            words = _tokens(q)
            dead = [t for t in words if not any(_matches(r, t) for r in pool)]
            # Plain words JLC never uses ("receptacle") are dropped; value words ("3.3v", "100nf") never are,
            # since dropping one would return parts with the wrong value.
            droppable = [t for t in dead if not t[0].isdigit()]
            kept = [t for t in words if t not in droppable]
            if droppable and len(droppable) == len(dead) and kept:
                rows = [r for r in pool if _matches(r, " ".join(kept))]
            if rows:
                notes.append(f"No part contained every word of {q!r}; ignored {', '.join(droppable)} (no part here "
                             f"contains it) and matched on: {' '.join(kept)}.")
            else:
                notes.append(f"{before} parts came back but none contained every word of {q!r}"
                             + (f"; no part at all contained: {', '.join(dead)}" if dead else "") + ".")
    if package:
        rows = [r for r in rows if (r["package"] or "").lower() == package.lower()] or rows
    if basic_only:
        rows = [r for r in rows if r["basic"]]
    if not rows:
        notes.append(NOT_FOUND_NOTE)
    return {"source": SOURCE, "as_of": at, "notes": notes, "total": len(rows), "parts": rows[:limit]}


# ---- ranking ---------------------------------------------------------------------------------------
def rank(parts: list[dict], qty_per_board: int, boards: int, extended_fee: float, package: str | None = None) -> list[dict]:
    """Deterministic ranking with a visible breakdown. Order (lexicographic):
    1 enough stock for the order (unknown stock ranks below known-sufficient, above known-short),
    2 package matches the requested one, 3 order cost = unit price at the order quantity x quantity
    + the extended-part loading fee (Basic and Preferred parts have none), 4 more stock first."""
    need = max(1, qty_per_board) * max(1, boards)
    out = []
    for p in parts:
        up = unit_price(p["tiers"], need)
        fee = 0.0 if (p["basic"] or p["preferred"]) else extended_fee
        cost = None if up is None else round(up * need + fee, 4)
        stock = p["stock"]
        stock_state = "unknown" if stock is None else ("ok" if stock >= need else "short")
        pkg_ok = (not package) or (p["package"] or "").lower() == package.lower()
        kind = "Basic" if p["basic"] else ("Preferred" if p["preferred"] else "Extended")
        why = [f"{kind}{'' if fee == 0 else f' (+${extended_fee:g} loading fee)'}",
               f"stock {stock if stock is not None else '?'} for {need} needed ({stock_state})",
               f"${up:.4f}/pc at {need} pcs" if up is not None else "no price"]
        if not pkg_ok:
            why.append(f"package {p['package']} != wanted {package}")
        key = ({"ok": 0, "unknown": 1, "short": 2}[stock_state], 0 if pkg_ok else 1,
               cost if cost is not None else math.inf, -(stock or 0))
        out.append({**p, "need": need, "unit_price": up, "order_cost": cost, "kind": kind,
                    "stock_state": stock_state, "package_ok": pkg_ok, "why": "; ".join(why), "_key": key})
    out.sort(key=lambda r: r["_key"])
    for i, r in enumerate(out, 1):
        r["rank"] = i
        del r["_key"]
    return out

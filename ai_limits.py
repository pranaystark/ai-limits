#!/usr/bin/env python3
"""AGY + Grok remaining-quota widget. Fetch on start and Refresh only."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango, PangoCairo

APP_ID = "org.local.AiLimits"
CACHE_DIR = Path.home() / ".cache" / "ai-limits"
STATE_PATH = CACHE_DIR / "state.json"
CSS_PATH = Path(__file__).with_name("style.css")
ICON_DIR = Path(__file__).with_name("icons")
HOME_BIN = Path.home() / ".local" / "bin"

AGY_ROW = re.compile(
    r"^(?P<group>.+?)\s+(?P<window>Weekly|Five Hour) Limit Remaining\s+"
    r"(?P<pct>\d+(?:\.\d+)?)\s*%\s+(?P<reset>\S+)",
    re.I,
)


def _path_env() -> dict[str, str]:
    env = os.environ.copy()
    extra = str(HOME_BIN)
    path = env.get("PATH", "")
    if extra not in path.split(":"):
        env["PATH"] = extra + ":" + path
    return env


def which(name: str) -> str:
    found = shutil.which(name, path=_path_env()["PATH"])
    if found:
        return found
    fallback = HOME_BIN / name
    if fallback.is_file():
        return str(fallback)
    raise FileNotFoundError(f"{name} not on PATH")


def parse_agy(text: str) -> list[dict]:
    rows: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip().replace("\t", "  ")
        if not line or line.lower().startswith("quota"):
            continue
        m = AGY_ROW.search(line)
        if not m:
            continue
        window = "5h" if m.group("window").lower().startswith("five") else "1w"
        rows.append(
            {
                "group": re.sub(r"\s+", " ", m.group("group")).strip(),
                "window": window,
                "windowLabel": "Five hour" if window == "5h" else "Weekly",
                "remaining": float(m.group("pct")),
                "resetsAt": m.group("reset").strip(),
            }
        )
    return rows


def parse_grok(payload: dict) -> list[dict]:
    rows: list[dict] = []
    for report in payload.get("reports") or []:
        if report.get("provider") != "xai-oauth":
            continue
        for limit in report.get("limits") or []:
            amount = limit.get("amount") or {}
            window = limit.get("window") or {}
            remaining = amount.get("remaining")
            if remaining is None and amount.get("remainingFraction") is not None:
                remaining = round(float(amount["remainingFraction"]) * 100)
            rows.append(
                {
                    "id": limit.get("id") or "",
                    "label": limit.get("label") or "Grok",
                    "window": window.get("id") or "1w",
                    "windowLabel": window.get("label") or "Weekly",
                    "remaining": float(remaining if remaining is not None else 0),
                    "resetsAt": window.get("resetsAt"),
                    "headline": (limit.get("id") or "").endswith(":credits:1w"),
                }
            )
    rows.sort(key=lambda r: (not r.get("headline"), r["remaining"]))
    return rows


def _run(cmd: list[str], timeout: int) -> str:
    r = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_path_env(),
    )
    if r.returncode != 0:
        err = (r.stderr or r.stdout or f"exit {r.returncode}").strip()
        raise RuntimeError(err[:400])
    return r.stdout


def fetch_agy() -> list[dict]:
    out = _run([which("agy"), "-p", "/usage"], timeout=60)
    rows = parse_agy(out)
    if not rows:
        raise RuntimeError("agy -p /usage returned no quota rows")
    return rows


def fetch_grok() -> list[dict]:
    out = _run(
        [which("omp"), "usage", "--json", "--provider", "xai-oauth"],
        timeout=40,
    )
    start = out.find("{")
    if start < 0:
        raise RuntimeError("omp usage returned no JSON")
    payload = json.loads(out[start:])
    rows = parse_grok(payload)
    if not rows:
        raise RuntimeError("omp usage had no xai-oauth limits")
    return rows


def tone(remaining: float) -> str:
    if remaining < 20:
        return "crit"
    if remaining < 50:
        return "warn"
    return "ok"


def parse_reset(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ms = float(value)
        if ms > 1e12:
            ms /= 1000.0
        return datetime.fromtimestamp(ms, tz=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return parse_reset(int(text))
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def rel_reset(value) -> str:
    dt = parse_reset(value)
    if dt is None:
        return ""
    sec = (dt - datetime.now(timezone.utc)).total_seconds()
    if sec <= 0:
        return "reset due"
    if sec < 3600:
        return f"resets in {int(sec // 60)}m"
    if sec < 86400:
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        return f"resets in {h}h {m}m" if m else f"resets in {h}h"
    days = int(sec // 86400)
    return f"resets in {days}d"


def clock(ts: float | None) -> str:
    if not ts:
        return ""
    local = datetime.fromtimestamp(ts).astimezone().strftime("%H:%M")
    return f"Fetched {local}"


@dataclass
class State:
    updatedAt: float | None = None
    agy: list[dict] = field(default_factory=list)
    grok: list[dict] = field(default_factory=list)
    agyError: str | None = None
    grokError: str | None = None

    def save(self) -> None:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        tmp.replace(STATE_PATH)

    @classmethod
    def load(cls) -> "State":
        if not STATE_PATH.exists():
            return cls()
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            return cls(
                updatedAt=data.get("updatedAt"),
                agy=data.get("agy") or [],
                grok=data.get("grok") or [],
                agyError=data.get("agyError"),
                grokError=data.get("grokError"),
            )
        except (OSError, json.JSONDecodeError):
            return cls()

    def headline_agy(self) -> tuple[float | None, str]:
        gemini = [r for r in self.agy if "gemini" in r.get("group", "").lower()]
        five = [r for r in gemini if r.get("window") == "5h"]
        if five:
            return five[0]["remaining"], "Gemini 5h"
        pool = gemini or self.agy
        if not pool:
            return None, "AGY"
        row = min(pool, key=lambda r: r["remaining"])
        label = f"{row['group']} {row['windowLabel'].lower()}"
        return row["remaining"], label

    def headline_grok(self) -> tuple[float | None, str]:
        if not self.grok:
            return None, "Grok"
        head = next((r for r in self.grok if r.get("headline")), self.grok[0])
        return head["remaining"], head.get("label") or "SuperGrok"


def collect(targets: set[str] | None = None, previous: State | None = None) -> State:
    wanted = targets or {"agy", "grok"}
    base = previous or State.load()
    state = State(
        updatedAt=datetime.now().timestamp(),
        agy=list(base.agy),
        grok=list(base.grok),
        agyError=base.agyError if "agy" not in wanted else None,
        grokError=base.grokError if "grok" not in wanted else None,
    )
    jobs = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        if "agy" in wanted:
            jobs["agy"] = pool.submit(fetch_agy)
        if "grok" in wanted:
            jobs["grok"] = pool.submit(fetch_grok)
        if "agy" in jobs:
            try:
                state.agy = jobs["agy"].result()
            except Exception as exc:  # noqa: BLE001
                state.agyError = str(exc)
        if "grok" in jobs:
            try:
                state.grok = jobs["grok"].result()
            except Exception as exc:  # noqa: BLE001
                state.grokError = str(exc)
    return state


class Ring(Gtk.DrawingArea):
    def __init__(self, kind: str) -> None:
        super().__init__()
        self._kind = kind
        self._frac = 0.0
        self._text = "—"
        self._tone = "ok"
        self.set_content_width(96)
        self.set_content_height(96)
        self.set_draw_func(self._draw)

    def set_value(self, remaining: float | None) -> None:
        if remaining is None:
            self._frac = 0.0
            self._text = "—"
            self._tone = "ok"
        else:
            self._frac = max(0.0, min(1.0, remaining / 100.0))
            self._text = f"{int(round(remaining))}"
            self._tone = tone(remaining)
        self.queue_draw()

    def _colors(self):
        if self._tone == "crit":
            return (0.95, 0.38, 0.35, 1), (0.95, 0.38, 0.35, 0.18)
        if self._tone == "warn":
            return (0.96, 0.78, 0.25, 1), (0.96, 0.78, 0.25, 0.18)
        if self._kind == "grok":
            return (0.88, 0.47, 0.22, 1), (0.88, 0.47, 0.22, 0.18)
        return (0.08, 0.72, 0.65, 1), (0.08, 0.72, 0.65, 0.18)

    def _draw(self, _area, cr, width: int, height: int) -> None:
        fg, track = self._colors()
        cx, cy = width / 2, height / 2
        radius = min(width, height) / 2 - 8
        cr.set_line_width(7)
        cr.set_line_cap(1)
        cr.set_source_rgba(*track)
        cr.arc(cx, cy, radius, 0, math.tau)
        cr.stroke()
        if self._frac > 0:
            cr.set_source_rgba(*fg)
            cr.arc(cx, cy, radius, -math.pi / 2, -math.pi / 2 + math.tau * self._frac)
            cr.stroke()

        layout = PangoCairo.create_layout(cr)
        desc = Pango.FontDescription("Sans Bold 20")
        layout.set_font_description(desc)
        layout.set_text(self._text)
        tw, th = layout.get_pixel_size()
        cr.set_source_rgba(*fg)
        cr.move_to(cx - tw / 2, cy - th / 2 - 6)
        PangoCairo.show_layout(cr, layout)

        layout2 = PangoCairo.create_layout(cr)
        layout2.set_font_description(Pango.FontDescription("Sans 8"))
        layout2.set_text("% left")
        tw2, th2 = layout2.get_pixel_size()
        cr.set_source_rgba(fg[0], fg[1], fg[2], 0.7)
        cr.move_to(cx - tw2 / 2, cy + 12)
        PangoCairo.show_layout(cr, layout2)


class Hero(Gtk.Box):
    def __init__(self, kind: str, title: str, on_refresh) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.add_css_class("hero")
        self.add_css_class(kind)
        self.set_hexpand(True)
        self._kind = kind
        kicker = Gtk.Label(label=title, xalign=0.5)
        kicker.add_css_class("hero-kicker")
        self.ring = Ring(kind)
        self.refresh_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        self.refresh_btn.set_tooltip_text(f"Refresh {title}")
        self.refresh_btn.add_css_class("flat")
        self.refresh_btn.add_css_class("pie-refresh")
        self.refresh_btn.set_halign(Gtk.Align.END)
        self.refresh_btn.set_valign(Gtk.Align.START)
        self.refresh_btn.connect("clicked", lambda *_: on_refresh())
        overlay = Gtk.Overlay()
        overlay.set_child(self.ring)
        overlay.add_overlay(self.refresh_btn)
        self.sub = Gtk.Label(label="waiting", xalign=0.5)
        self.sub.add_css_class("hero-sub")
        self.sub.set_wrap(True)
        self.append(kicker)
        self.append(overlay)
        self.append(self.sub)

    def set_busy(self, busy: bool) -> None:
        self.refresh_btn.set_sensitive(not busy)
        if busy:
            self.add_css_class("busy")
        else:
            self.remove_css_class("busy")

    def update(self, remaining: float | None, subtitle: str, error: str | None) -> None:
        if error and remaining is None:
            self.ring.set_value(None)
            self.sub.set_text(error.split("\n")[0][:80])
            return
        self.ring.set_value(remaining)
        self.sub.set_text(subtitle)


class BarRow(Gtk.Box):
    def __init__(self, kind: str) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        self._kind = kind
        self._reset_raw = None
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.title = Gtk.Label(xalign=0)
        self.title.add_css_class("row-title")
        self.title.set_hexpand(True)
        self.pct = Gtk.Label(xalign=1)
        self.pct.add_css_class("row-pct")
        head.append(self.title)
        head.append(self.pct)
        self.bar = Gtk.ProgressBar()
        self.bar.add_css_class("limits-bar")
        self.bar.add_css_class(kind)
        self.meta = Gtk.Label(xalign=0)
        self.meta.add_css_class("row-meta")
        self.append(head)
        self.append(self.bar)
        self.append(self.meta)

    def update(self, title: str, remaining: float, reset) -> None:
        self._reset_raw = reset
        t = tone(remaining)
        self.title.set_text(title)
        self.pct.set_text(f"{int(round(remaining))}%")
        for cls in ("tone-ok", "tone-warn", "tone-crit"):
            self.pct.remove_css_class(cls)
        self.pct.add_css_class(f"tone-{t}")
        for cls in ("warn", "crit"):
            self.bar.remove_css_class(cls)
        if t != "ok":
            self.bar.add_css_class(t)
        self.bar.set_fraction(max(0.0, min(1.0, remaining / 100.0)))
        self.tick()

    def tick(self) -> None:
        self.meta.set_text(rel_reset(self._reset_raw))


SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionMovieName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="XAyatanaLabel" type="s" access="read"/>
    <property name="XAyatanaLabelGuide" type="s" access="read"/>
    <method name="ContextMenu">
      <arg type="i" name="x" direction="in"/>
      <arg type="i" name="y" direction="in"/>
    </method>
    <method name="Activate">
      <arg type="i" name="x" direction="in"/>
      <arg type="i" name="y" direction="in"/>
    </method>
    <method name="SecondaryActivate">
      <arg type="i" name="x" direction="in"/>
      <arg type="i" name="y" direction="in"/>
    </method>
    <method name="Scroll">
      <arg type="i" name="delta" direction="in"/>
      <arg type="s" name="orientation" direction="in"/>
    </method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewAttentionIcon"/>
    <signal name="NewOverlayIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus">
      <arg type="s" name="status"/>
    </signal>
    <signal name="XAyatanaNewLabel">
      <arg type="s" name="label"/>
      <arg type="s" name="guide"/>
    </signal>
  </interface>
</node>
"""


def robot_pixmap(rgba: tuple[float, float, float, float]):
    size = 22
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(surf)
    cr.set_source_rgba(*rgba)
    cr.set_line_width(1.4)
    cr.set_line_cap(1)
    cr.move_to(11, 3.2)
    cr.line_to(11, 6.2)
    cr.stroke()
    cr.arc(11, 2.6, 1.15, 0, math.tau)
    cr.fill()
    cr.set_line_width(0)
    _round_rect(cr, 4.2, 6.0, 13.6, 11.2, 2.4)
    cr.fill()
    cr.set_source_rgb(1, 1, 1)
    cr.arc(8.2, 11.0, 1.45, 0, math.tau)
    cr.fill()
    cr.arc(13.8, 11.0, 1.45, 0, math.tau)
    cr.fill()
    cr.set_source_rgba(0, 0, 0, 0.55)
    cr.arc(8.2, 11.0, 0.7, 0, math.tau)
    cr.fill()
    cr.arc(13.8, 11.0, 0.7, 0, math.tau)
    cr.fill()
    cr.set_source_rgba(*rgba)
    _round_rect(cr, 7.4, 17.6, 7.2, 3.0, 1.1)
    cr.fill()
    raw = bytes(surf.get_data())
    stride = surf.get_stride()
    out = bytearray()
    for y in range(size):
        row = raw[y * stride : y * stride + size * 4]
        for x in range(size):
            b, g, r, a = row[x * 4 : x * 4 + 4]
            out.extend((a, r, g, b))
    return [(size, size, bytes(out))]


def _round_rect(cr, x, y, w, h, r) -> None:
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def tone_rgba(kind: str, remaining: float | None):
    t = "ok" if remaining is None else tone(remaining)
    if t == "crit":
        return (0.95, 0.38, 0.35, 1.0)
    if t == "warn":
        return (0.96, 0.78, 0.25, 1.0)
    return (0.08, 0.72, 0.65, 1.0)


def svg_pixmap(path: Path, size: int = 22):
    pb = GdkPixbuf.Pixbuf.new_from_file_at_size(str(path), size, size)
    w, h = pb.get_width(), pb.get_height()
    pixels = pb.get_pixels()
    nch = pb.get_n_channels()
    stride = pb.get_rowstride()
    out = bytearray()
    for y in range(h):
        for x in range(w):
            i = y * stride + x * nch
            r, g, b = pixels[i], pixels[i + 1], pixels[i + 2]
            a = pixels[i + 3] if nch == 4 else 255
            out.extend((a, r, g, b))
    return [(w, h, bytes(out))]


def brand_pixmap(kind: str, remaining: float | None):
    if kind == "agy":
        path = ICON_DIR / "antigravity.svg"
    else:
        png = ICON_DIR / "grok.png"
        path = png if png.exists() else ICON_DIR / "grok.svg"
    try:
        return svg_pixmap(path)
    except Exception:
        return robot_pixmap(tone_rgba(kind, remaining))


DBUSMENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg type="i" name="parentId" direction="in"/>
      <arg type="i" name="recursionDepth" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="u" name="revision" direction="out"/>
      <arg type="(ia{sv}av)" name="layout" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="a(ia{sv})" name="properties" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="name" direction="in"/>
      <arg type="v" name="value" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="eventId" direction="in"/>
      <arg type="v" name="data" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg type="a(isvu)" name="events" direction="in"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg type="i" name="id" direction="in"/>
      <arg type="b" name="needUpdate" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="ai" name="updatesNeeded" direction="out"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg type="a(ia{sv})" name="updatedProps"/>
      <arg type="a(ias)" name="removedProps"/>
    </signal>
    <signal name="LayoutUpdated">
      <arg type="u" name="revision"/>
      <arg type="i" name="parent"/>
    </signal>
  </interface>
</node>
"""


class GlanceMenu:
    def __init__(self, app: Adw.Application, path: str, kind: str) -> None:
        self._app = app
        self._path = path
        self._kind = kind
        self._conn: Gio.DBusConnection | None = None
        self._rev = 1
        self._items: dict[int, dict] = {0: {"children-display": "submenu"}}
        self._actions: dict[int, str] = {}
        self._root_children: list[int] = []

    def export(self, connection: Gio.DBusConnection) -> None:
        self._conn = connection
        info = Gio.DBusNodeInfo.new_for_xml(DBUSMENU_XML)
        connection.register_object(
            self._path,
            info.interfaces[0],
            self._on_method,
            self._on_get,
            None,
        )

    def set_state(self, state: State) -> None:
        items: dict[int, dict] = {}
        actions: dict[int, str] = {}
        children: list[int] = []
        nid = 1

        def add(props: dict, action: str | None = None) -> None:
            nonlocal nid
            iid = nid
            nid += 1
            items[iid] = props
            children.append(iid)
            if action:
                actions[iid] = action

        if self._kind == "agy":
            remaining, sub = state.headline_agy()
            pct = "—" if remaining is None else f"{int(round(remaining))}%"
            add({"label": f"AGY  {pct}  {sub}", "enabled": False})
            add({"type": "separator"})
            for row in state.agy:
                add(
                    {
                        "label": f"{row['group']}  {row['windowLabel']}  {int(row['remaining'])}%",
                        "enabled": False,
                    }
                )
            if state.agyError:
                add({"label": state.agyError[:80], "enabled": False})
            add({"type": "separator"})
            add({"label": "Refresh AGY"}, "refresh-agy")
        else:
            remaining, sub = state.headline_grok()
            pct = "—" if remaining is None else f"{int(round(remaining))}%"
            add({"label": f"Grok  {pct}  {sub}", "enabled": False})
            add({"type": "separator"})
            for row in state.grok:
                add(
                    {
                        "label": f"{row['label']}  {int(row['remaining'])}%",
                        "enabled": False,
                    }
                )
            if state.grokError:
                add({"label": state.grokError[:80], "enabled": False})
            add({"type": "separator"})
            add({"label": "Refresh Grok"}, "refresh-grok")
        add({"label": "Open card"}, "show")
        items[0] = {"children-display": "submenu"}
        self._root_children = children
        self._items = items
        self._actions = actions
        self._rev += 1
        if self._conn is not None:
            self._conn.emit_signal(
                None,
                self._path,
                "com.canonical.dbusmenu",
                "LayoutUpdated",
                GLib.Variant("(ui)", (self._rev, 0)),
            )

    def _pack(self, iid: int):
        props = {k: _menu_var(k, v) for k, v in self._items.get(iid, {}).items() if k != "children"}
        kids = self._root_children if iid == 0 else []
        child_vars = [GLib.Variant("(ia{sv}av)", self._pack(cid)) for cid in kids]
        return (iid, props, child_vars)

    def _on_method(self, _c, _s, _p, _i, method, params, invocation) -> None:
        if method == "GetLayout":
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (self._rev, self._pack(0))))
        elif method == "GetGroupProperties":
            ids, names = params.unpack()
            out = []
            for iid in ids:
                props = self._items.get(int(iid), {})
                packed = {k: _menu_var(k, v) for k, v in props.items() if k != "children" and (not names or k in names)}
                out.append((int(iid), packed))
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (out,)))
        elif method == "GetProperty":
            iid, name = params.unpack()
            props = self._items.get(int(iid), {})
            invocation.return_value(GLib.Variant("(v)", (_menu_var(name, props.get(name, "")),)))
        elif method == "Event":
            iid, event_id, _data, _ts = params.unpack()
            if event_id == "clicked":
                action = self._actions.get(int(iid))
                if action:
                    self._app.lookup_action(action).activate(None)
            invocation.return_value(None)
        elif method == "EventGroup":
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            win = self._app.win
            if win is not None:
                self.set_state(win.state)
            invocation.return_value(GLib.Variant("(b)", (True,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_error_literal(
                Gio.dbus_error_quark(),
                Gio.DBusError.UNKNOWN_METHOD,
                method,
            )

    def _on_get(self, _c, _s, _p, _i, prop):
        table = {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
            "IconThemePath": GLib.Variant("as", []),
        }
        return table.get(prop)


def _menu_var(key: str, value):
    if key in {"enabled", "visible"}:
        return GLib.Variant("b", bool(value) if not isinstance(value, bool) else value)
    if isinstance(value, bool):
        return GLib.Variant("b", value)
    return GLib.Variant("s", str(value))


class StatusIcon:
    def __init__(self, app: Adw.Application, path: str, item_id: str, kind: str, title: str) -> None:
        self._app = app
        self._path = path
        self._item_id = item_id
        self._kind = kind
        self._title = title
        self._conn: Gio.DBusConnection | None = None
        self._label = "—%"
        self._guide = "100%"
        self._tip = title
        self._menu_path = f"{path}/menu"
        self._menu = GlanceMenu(app, self._menu_path, kind)
        self._pixmap = brand_pixmap(kind, 100)

    def export(self, connection: Gio.DBusConnection) -> None:
        self._conn = connection
        self._menu.export(connection)
        info = Gio.DBusNodeInfo.new_for_xml(SNI_XML)
        connection.register_object(
            self._path,
            info.interfaces[0],
            self._on_method,
            self._on_get,
            None,
        )
        Gio.DBusProxy.new(
            connection,
            Gio.DBusProxyFlags.NONE,
            None,
            "org.kde.StatusNotifierWatcher",
            "/StatusNotifierWatcher",
            "org.kde.StatusNotifierWatcher",
            None,
            self._on_watcher,
        )

    def _on_watcher(self, _source, result) -> None:
        try:
            proxy = Gio.DBusProxy.new_finish(result)
            proxy.call(
                "RegisterStatusNotifierItem",
                GLib.Variant("(s)", (self._path,)),
                Gio.DBusCallFlags.NONE,
                4000,
                None,
                None,
            )
        except Exception:
            pass

    def set_value(self, remaining: float | None, subtitle: str) -> None:
        self._label = "—%" if remaining is None else f"{int(round(remaining))}%"
        extra = subtitle.strip()
        self._tip = f"{self._title} {self._label}" + (f" · {extra}" if extra else "")
        self._pixmap = brand_pixmap(self._kind, remaining)
        if self._app.win is not None:
            self._menu.set_state(self._app.win.state)
        if self._conn is None:
            return
        self._conn.emit_signal(
            None,
            self._path,
            "org.kde.StatusNotifierItem",
            "XAyatanaNewLabel",
            GLib.Variant("(ss)", (self._label, self._guide)),
        )
        self._conn.emit_signal(None, self._path, "org.kde.StatusNotifierItem", "NewIcon", None)
        self._conn.emit_signal(None, self._path, "org.kde.StatusNotifierItem", "NewToolTip", None)
        self._conn.emit_signal(
            None,
            self._path,
            "org.freedesktop.DBus.Properties",
            "PropertiesChanged",
            GLib.Variant(
                "(sa{sv}as)",
                (
                    "org.kde.StatusNotifierItem",
                    {
                        "XAyatanaLabel": GLib.Variant("s", self._label),
                        "IconPixmap": GLib.Variant("a(iiay)", self._pixmap),
                        "ToolTip": GLib.Variant(
                            "(sa(iiay)ss)",
                            ("", [], self._tip, "Click card · middle refresh"),
                        ),
                    },
                    [],
                ),
            ),
        )

    def _on_method(self, _c, _s, _p, _i, method, _params, invocation) -> None:
        if method == "Activate":
            self._app.lookup_action("show").activate(None)
        elif method == "SecondaryActivate":
            self._app.lookup_action(f"refresh-{self._kind}").activate(None)
        elif method == "ContextMenu":
            self._app.lookup_action(f"refresh-{self._kind}").activate(None)
        invocation.return_value(None)

    def _on_get(self, _c, _s, _p, _i, prop):
        table = {
            "Category": GLib.Variant("s", "SystemServices"),
            "Id": GLib.Variant("s", self._item_id),
            "Title": GLib.Variant("s", self._title),
            "Status": GLib.Variant("s", "Active"),
            "WindowId": GLib.Variant("i", 0),
            "IconThemePath": GLib.Variant("s", ""),
            "Menu": GLib.Variant("o", self._menu_path),
            "ItemIsMenu": GLib.Variant("b", False),
            "IconName": GLib.Variant("s", ""),
            "IconPixmap": GLib.Variant("a(iiay)", self._pixmap),
            "OverlayIconName": GLib.Variant("s", ""),
            "OverlayIconPixmap": GLib.Variant("a(iiay)", []),
            "AttentionIconName": GLib.Variant("s", ""),
            "AttentionIconPixmap": GLib.Variant("a(iiay)", []),
            "AttentionMovieName": GLib.Variant("s", ""),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", ("", [], self._tip, "Click card · middle/right refresh")),
            "XAyatanaLabel": GLib.Variant("s", self._label),
            "XAyatanaLabelGuide": GLib.Variant("s", self._guide),
        }
        return table.get(prop)


class LimitsWindow(Gtk.ApplicationWindow):
    def __init__(self, app: Adw.Application) -> None:
        super().__init__(application=app, title="AI Limits")
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_default_size(360, 520)
        self.add_css_class("limits-widget")
        self.set_hide_on_close(True)
        self._inflight: set[str] = set()
        self.state = State.load()
        self._rows: list[BarRow] = []

        self.refresh_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        self.refresh_btn.set_tooltip_text("Refresh AGY and Grok")
        self.refresh_btn.add_css_class("flat")
        self.refresh_btn.add_css_class("icon-btn")
        self.refresh_btn.connect("clicked", lambda *_: self.refresh())
        hide_btn = Gtk.Button.new_from_icon_name("window-close-symbolic")
        hide_btn.set_tooltip_text("Hide")
        hide_btn.add_css_class("flat")
        hide_btn.add_css_class("icon-btn")
        hide_btn.connect("clicked", lambda *_: self.set_visible(False))
        self.spinner = Adw.Spinner()
        self.spinner.set_visible(False)

        title = Gtk.Label(label="LIMITS", xalign=0)
        title.add_css_class("chrome-title")
        title.set_hexpand(True)
        chrome = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        chrome.add_css_class("chrome")
        chrome.append(title)
        chrome.append(self.spinner)
        chrome.append(self.refresh_btn)
        chrome.append(hide_btn)

        self.agy_hero = Hero("agy", "AGY", lambda: self.refresh("agy"))
        self.grok_hero = Hero("grok", "GROK", lambda: self.refresh("grok"))
        heroes = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        heroes.set_homogeneous(True)
        heroes.append(self.agy_hero)
        heroes.append(self.grok_hero)

        self.details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        scrolled.set_child(self.details)

        self.footer = Gtk.Label(xalign=0)
        self.footer.add_css_class("footer")
        self.footer.set_wrap(True)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        body.add_css_class("widget-card")
        body.append(chrome)
        body.append(heroes)
        body.append(scrolled)
        body.append(self.footer)
        self.set_child(body)

        drag = Gtk.GestureClick()
        drag.connect("pressed", self._on_drag)
        title.add_controller(drag)

        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_key)
        self.add_controller(key)

        GLib.timeout_add_seconds(30, self._tick)
        self._render(self.state)

    def _on_drag(self, gesture, _n, x, y) -> None:
        surface = self.get_surface()
        device = gesture.get_current_event_device()
        event = gesture.get_current_event()
        if surface is None or device is None or event is None:
            return
        if isinstance(surface, Gdk.Toplevel):
            surface.begin_move(device, 1, x, y, event.get_time())

    def _on_key(self, _c, keyval, _code, _mod) -> bool:
        if keyval in (Gdk.KEY_F5,):
            self.refresh()
            return True
        if keyval in (Gdk.KEY_r, Gdk.KEY_R) and (_mod & Gdk.ModifierType.CONTROL_MASK):
            self.refresh()
            return True
        if keyval == Gdk.KEY_Escape:
            self.set_visible(False)
            return True
        return False

    def _tick(self) -> bool:
        for row in self._rows:
            row.tick()
        agy_r, agy_s = self.state.headline_agy()
        grok_r, grok_s = self.state.headline_grok()
        if agy_r is not None:
            extra = rel_reset(
                next((x.get("resetsAt") for x in self.state.agy if x["remaining"] == agy_r), None)
            )
            self.agy_hero.sub.set_text(" · ".join(p for p in (agy_s, extra) if p))
        if grok_r is not None:
            extra = rel_reset(
                next((x.get("resetsAt") for x in self.state.grok if x.get("headline")), None)
            )
            self.grok_hero.sub.set_text(" · ".join(p for p in (grok_s, extra) if p))
        return True

    def _set_busy(self, targets: set[str], busy: bool) -> None:
        if busy:
            self._inflight |= targets
        else:
            self._inflight -= targets
        self.refresh_btn.set_sensitive(not self._inflight)
        self.spinner.set_visible(bool(self._inflight))
        self.agy_hero.set_busy("agy" in self._inflight)
        self.grok_hero.set_busy("grok" in self._inflight)
        if busy:
            names = " and ".join(t.upper() for t in sorted(targets))
            self.footer.set_text(f"Fetching {names}…")

    def _render(self, state: State) -> None:
        self.state = state
        agy_r, agy_s = state.headline_agy()
        grok_r, grok_s = state.headline_grok()
        agy_extra = ""
        if agy_r is not None and state.agy:
            agy_extra = rel_reset(min(state.agy, key=lambda r: r["remaining"]).get("resetsAt"))
        grok_extra = ""
        if grok_r is not None:
            grok_extra = rel_reset(
                next((r.get("resetsAt") for r in state.grok if r.get("headline")), None)
            )
        self.agy_hero.update(
            agy_r,
            " · ".join(p for p in (agy_s, agy_extra) if p) or "no data",
            state.agyError,
        )
        self.grok_hero.update(
            grok_r,
            " · ".join(p for p in (grok_s, grok_extra) if p) or "no data",
            state.grokError,
        )

        while child := self.details.get_first_child():
            self.details.remove(child)
        self._rows.clear()

        if state.agy:
            k = Gtk.Label(label="AGY", xalign=0)
            k.add_css_class("section-kicker")
            self.details.append(k)
            groups: dict[str, list[dict]] = {}
            for row in state.agy:
                groups.setdefault(row["group"], []).append(row)
            for group, items in groups.items():
                gl = Gtk.Label(label=group, xalign=0)
                gl.add_css_class("group-label")
                self.details.append(gl)
                order = {"5h": 0, "1w": 1}
                for item in sorted(items, key=lambda r: order.get(r["window"], 9)):
                    bar = BarRow("agy")
                    bar.update(item["windowLabel"], item["remaining"], item.get("resetsAt"))
                    self.details.append(bar)
                    self._rows.append(bar)
        elif state.agyError:
            k = Gtk.Label(label="AGY", xalign=0)
            k.add_css_class("section-kicker")
            err = Gtk.Label(label=state.agyError, xalign=0, wrap=True)
            err.add_css_class("tone-crit")
            self.details.append(k)
            self.details.append(err)

        if state.grok:
            k = Gtk.Label(label="GROK", xalign=0)
            k.add_css_class("section-kicker")
            self.details.append(k)
            for item in state.grok:
                bar = BarRow("grok")
                bar.update(item["label"], item["remaining"], item.get("resetsAt"))
                self.details.append(bar)
                self._rows.append(bar)
        elif state.grokError:
            k = Gtk.Label(label="GROK", xalign=0)
            k.add_css_class("section-kicker")
            err = Gtk.Label(label=state.grokError, xalign=0, wrap=True)
            err.add_css_class("tone-crit")
            self.details.append(k)
            self.details.append(err)

        bits = [clock(state.updatedAt)]
        if state.agyError or state.grokError:
            bits.append("one source failed")
        self.footer.set_text(" · ".join(b for b in bits if b) or "Not fetched yet")
        app = self.get_application()
        if app is not None:
            app.update_tray(state)

    def refresh(self, target: str = "all") -> None:
        wanted = {"agy", "grok"} if target == "all" else {target}
        wanted -= self._inflight
        if not wanted:
            return
        self._set_busy(wanted, True)

        def work() -> None:
            state = collect(wanted, self.state)
            state.save()
            GLib.idle_add(self._on_fetched, state, wanted)

        threading.Thread(target=work, daemon=True).start()

    def _on_fetched(self, state: State, targets: set[str]) -> bool:
        self._set_busy(targets, False)
        self._render(state)
        return False


class LimitsApp(Adw.Application):
    def __init__(self, start_hidden: bool) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )
        self._start_hidden = start_hidden
        self.win: LimitsWindow | None = None
        self.tray_agy = StatusIcon(self, "/StatusNotifierItem/agy", "ai-limits-agy", "agy", "AGY")
        self.tray_grok = StatusIcon(self, "/StatusNotifierItem/grok", "ai-limits-grok", "grok", "Grok")
        self.hold()
        for name, handler in (
            ("toggle", self._toggle),
            ("refresh", lambda *_: self._refresh("all")),
            ("refresh-agy", lambda *_: self._refresh("agy")),
            ("refresh-grok", lambda *_: self._refresh("grok")),
            ("show", self._show),
            ("hide", self._hide),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)

    def do_startup(self) -> None:  # noqa: N802
        Adw.Application.do_startup(self)
        _load_css()

    def do_dbus_register(self, connection, object_path) -> bool:  # noqa: N802
        ok = Adw.Application.do_dbus_register(self, connection, object_path)
        self.tray_agy.export(connection)
        self.tray_grok.export(connection)
        return ok

    def update_tray(self, state: State) -> None:
        agy, asub = state.headline_agy()
        grok, gsub = state.headline_grok()
        self.tray_agy.set_value(agy, asub)
        self.tray_grok.set_value(grok, gsub)

    def do_activate(self) -> None:  # noqa: N802
        if self.win is None:
            self.win = LimitsWindow(self)
            if not self._start_hidden:
                self.win.present()
            self.win.refresh()
        elif not self._start_hidden:
            self.win.present()
        self._start_hidden = False

    def _toggle(self, *_args) -> None:
        if self.win is None:
            self._start_hidden = False
            self.activate()
            return
        if self.win.is_visible():
            self.win.set_visible(False)
        else:
            self.win.present()
    def _show(self, *_args) -> None:
        if self.win is None:
            self._start_hidden = False
            self.activate()
        if self.win is not None:
            self.win.set_visible(True)
            self.win.present()

    def _hide(self, *_args) -> None:
        if self.win is not None:
            self.win.set_visible(False)

    def _refresh(self, target: str = "all") -> None:
        if self.win is None:
            self.activate()
            if self.win is not None and target != "all":
                self.win.refresh(target)
        else:
            self.win.refresh(target)


def _load_css() -> None:
    display = Gdk.Display.get_default()
    if display is None or not CSS_PATH.exists():
        return
    css = Gtk.CssProvider()
    css.load_from_path(str(CSS_PATH))
    Gtk.StyleContext.add_provider_for_display(
        display,
        css,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )


def main() -> int:
    if "--fetch" in sys.argv:
        wanted = set()
        if "--agy" in sys.argv:
            wanted.add("agy")
        if "--grok" in sys.argv:
            wanted.add("grok")
        collect(wanted or None, State.load()).save()
        return 0
    hidden = "--hidden" in sys.argv
    argv = [a for a in sys.argv if a not in ("--hidden", "--refresh", "--fetch", "--agy", "--grok")]
    app = LimitsApp(start_hidden=hidden)
    return app.run(argv)


if __name__ == "__main__":
    raise SystemExit(main())

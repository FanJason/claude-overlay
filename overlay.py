#!/usr/bin/env python3
"""Generate a share overlay card for a Claude Code session.

Reads the session transcript JSONL from ~/.claude/projects (and, when
available, statusline snapshots captured by statusline.py) and renders a
self-contained HTML page with two variants: a portrait story card and a
landscape footer strip.

Usage:
  python3 overlay.py                 # latest session on this machine
  python3 overlay.py --session ID    # specific session id
  python3 overlay.py --list          # list recent sessions
  python3 overlay.py --no-open       # don't open the result in a browser
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
SNAPSHOT_DIR = Path.home() / ".claude-overlay" / "sessions"
OUT_DIR = Path(__file__).parent / "out"

# A "day" runs from 4am to 4am local time, so a late-night session that runs
# past midnight still counts toward the day it started in.
DAY_CUTOFF_HOUR = 4


def day_start_ts(now: datetime | None = None) -> float:
    """Epoch seconds of the most recent 4am local boundary."""
    now = now or datetime.now().astimezone()
    start = now.replace(hour=DAY_CUTOFF_HOUR, minute=0, second=0, microsecond=0)
    if now < start:
        start -= timedelta(days=1)
    return start.timestamp()

ACCENT = "#D97757"  # Claude terracotta
ACCENT_LINE = "#FF631D"  # brighter, saturated terracotta for the route line
ROUTE_LINE_ROTATION = -15  # degrees counter-clockwise


# --------------------------------------------------------------------------
# Transcript parsing
# --------------------------------------------------------------------------

def find_transcripts() -> list[Path]:
    if not CLAUDE_PROJECTS.is_dir():
        return []
    files = list(CLAUDE_PROJECTS.glob("*/*.jsonl"))
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def parse_ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def parse_transcript(path: Path, since: float | None = None) -> dict:
    """Parse one transcript. When `since` is set, events timestamped before it
    are ignored — used to clip a session to the current 4am-to-4am day."""
    prompts = 0
    tool_calls = 0
    lines_added = 0
    lines_removed = 0
    models: dict[str, int] = {}
    requests: dict[str, dict] = {}  # requestId -> {out, first_ts, last_ts}
    timestamps: list[float] = []
    project = path.parent.name.split("-")[-1]

    for raw in path.open():
        try:
            o = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(o, dict):
            continue

        ts = o.get("timestamp")
        t = parse_ts(ts) if isinstance(ts, str) else None
        if since is not None and t is not None and t < since:
            continue
        if t:
            timestamps.append(t)

        cwd = o.get("cwd")
        if isinstance(cwd, str) and cwd:
            project = Path(cwd).name

        otype = o.get("type")

        if otype == "user" and not o.get("isMeta") and not o.get("isSidechain"):
            content = (o.get("message") or {}).get("content")
            is_tool_result = isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            )
            if not is_tool_result and o.get("userType") == "external":
                prompts += 1

        elif otype == "assistant":
            msg = o.get("message") or {}
            model = msg.get("model")
            if isinstance(model, str) and "synthetic" not in model:
                models[model] = models.get(model, 0) + 1
            content = msg.get("content")
            if isinstance(content, list):
                tool_calls += sum(
                    1
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "tool_use"
                )
            usage = msg.get("usage") or {}
            rid = o.get("requestId")
            if rid and t and isinstance(usage.get("output_tokens"), int):
                r = requests.setdefault(
                    rid, {"out": 0, "first_ts": t, "last_ts": t}
                )
                r["out"] = max(r["out"], usage["output_tokens"])
                r["first_ts"] = min(r["first_ts"], t)
                r["last_ts"] = max(r["last_ts"], t)

        patch = o.get("toolUseResult")
        if isinstance(patch, dict):
            for hunk in patch.get("structuredPatch") or []:
                for line in hunk.get("lines", []):
                    if line.startswith("+"):
                        lines_added += 1
                    elif line.startswith("-"):
                        lines_removed += 1

    # Cumulative output-token route, ordered by request completion time.
    ordered = sorted(requests.values(), key=lambda r: r["last_ts"])
    route: list[tuple[float, int]] = []
    cum = 0
    for r in ordered:
        cum += r["out"]
        route.append((r["last_ts"], cum))

    # API ("thinking") time approximated as the span of each request's
    # streamed chunks. The statusline snapshot overrides this when present.
    api_ms = sum((r["last_ts"] - r["first_ts"]) * 1000 for r in ordered)

    return {
        "session_id": path.stem,
        "project": project,
        "prompts": prompts,
        "tool_calls": tool_calls,
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "output_tokens": cum,
        "models": models,
        "route": route,
        "api_ms": api_ms,
        "wall_ms": (max(timestamps) - min(timestamps)) * 1000 if timestamps else 0,
        "cost_usd": None,
        "_requests": list(requests.values()),
        "_ts_min": min(timestamps) if timestamps else None,
        "_ts_max": max(timestamps) if timestamps else None,
    }


def parse_day(transcripts: list[Path], now: datetime | None = None) -> dict:
    """Aggregate every session active in the current 4am-to-4am day into one
    set of stats: summed lines/tokens/thinking-time and a route line that is
    cumulative output tokens across the whole day, ordered by time."""
    since = day_start_ts(now)
    all_requests: list[dict] = []
    lines_added = lines_removed = prompts = tool_calls = sessions = 0
    models: dict[str, int] = {}
    projects: set[str] = set()
    ts_min: float | None = None
    ts_max: float | None = None

    for p in transcripts:
        # mtime is the last write; if that predates the window the whole file
        # is yesterday's and can be skipped without opening it.
        if p.stat().st_mtime < since:
            continue
        s = parse_transcript(p, since=since)
        reqs = s["_requests"]
        contributed = bool(reqs) or s["lines_added"] or s["lines_removed"] \
            or s["prompts"] or s["tool_calls"]
        if not contributed:
            continue
        all_requests.extend(reqs)
        lines_added += s["lines_added"]
        lines_removed += s["lines_removed"]
        prompts += s["prompts"]
        tool_calls += s["tool_calls"]
        for m, c in s["models"].items():
            models[m] = models.get(m, 0) + c
        projects.add(s["project"])
        sessions += 1
        if s["_ts_min"] is not None:
            ts_min = s["_ts_min"] if ts_min is None else min(ts_min, s["_ts_min"])
        if s["_ts_max"] is not None:
            ts_max = s["_ts_max"] if ts_max is None else max(ts_max, s["_ts_max"])

    ordered = sorted(all_requests, key=lambda r: r["last_ts"])
    route: list[tuple[float, int]] = []
    cum = 0
    for r in ordered:
        cum += r["out"]
        route.append((r["last_ts"], cum))
    api_ms = sum((r["last_ts"] - r["first_ts"]) * 1000 for r in ordered)

    day = datetime.fromtimestamp(since)
    return {
        "session_id": day.strftime("%Y%m%d"),
        "project": day.strftime("%b %d").replace(" 0", " "),
        "sessions": sessions,
        "project_count": len(projects),
        "prompts": prompts,
        "tool_calls": tool_calls,
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "output_tokens": cum,
        "models": models,
        "route": route,
        "api_ms": api_ms,
        "wall_ms": (ts_max - ts_min) * 1000 if ts_min and ts_max else 0,
        "cost_usd": None,
    }


def apply_snapshot(stats: dict) -> dict:
    """Override approximations with canonical totals from the statusline tap."""
    snap_file = SNAPSHOT_DIR / f"{stats['session_id']}.ndjson"
    if not snap_file.is_file():
        return stats
    last = None
    for raw in snap_file.open():
        try:
            last = json.loads(raw)
        except json.JSONDecodeError:
            continue
    if not isinstance(last, dict):
        return stats
    cost = last.get("cost") or {}
    if isinstance(cost.get("total_api_duration_ms"), (int, float)):
        stats["api_ms"] = cost["total_api_duration_ms"]
    if isinstance(cost.get("total_duration_ms"), (int, float)):
        stats["wall_ms"] = cost["total_duration_ms"]
    if isinstance(cost.get("total_lines_added"), int):
        stats["lines_added"] = cost["total_lines_added"]
    if isinstance(cost.get("total_lines_removed"), int):
        stats["lines_removed"] = cost["total_lines_removed"]
    if isinstance(cost.get("total_cost_usd"), (int, float)):
        stats["cost_usd"] = cost["total_cost_usd"]
    return stats


def session_is_empty(stats: dict) -> bool:
    return stats["output_tokens"] == 0 and stats["lines_added"] == 0


def resolve_transcript(
    transcripts: list[Path],
    *,
    session_id: str | None = None,
    transcript_path: str | None = None,
    allow_fallback: bool = True,
) -> tuple[Path | None, Path | None]:
    """Pick a transcript path, optionally falling back to a richer sibling session.

    Returns (chosen_path, fallback_from_path). fallback_from_path is set when
    we substitute a prior session in the same project because the requested one
    has no output yet (common when rate-limited).
    """
    path: Path | None = None
    if transcript_path:
        tp = Path(transcript_path)
        if tp.is_file():
            path = tp
    elif session_id:
        matches = [p for p in transcripts if p.stem.startswith(session_id)]
        path = matches[0] if matches else None
    elif transcripts:
        path = transcripts[0]

    if path is None:
        return None, None

    stats = apply_snapshot(parse_transcript(path))
    if not allow_fallback or not session_is_empty(stats):
        return path, None

    siblings = sorted(
        path.parent.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for sib in siblings:
        if sib == path:
            continue
        s = apply_snapshot(parse_transcript(sib))
        if not session_is_empty(s):
            return sib, path

    return path, None


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------

def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def fmt_duration(ms: float) -> str:
    s = int(ms / 1000)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def fmt_thinking(ms: float) -> str:
    """Unit-labelled duration for the thinking-time metric: `Xm Xs`, and
    `Xh Xm` once it crosses an hour, so the unit is always explicit."""
    s = int(ms / 1000)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {sec}s"
    return f"{sec}s"


# --------------------------------------------------------------------------
# SVG route path (Catmull-Rom -> cubic Bezier, same as the canvas mockup)
# --------------------------------------------------------------------------

def route_points(route: list[tuple[float, int]], w: float, h: float, pad: float):
    if len(route) < 2:
        return [(pad, h - pad), (w - pad, h - pad)]
    t0, t1 = route[0][0], route[-1][0]
    vmax = route[-1][1] or 1
    tspan = (t1 - t0) or 1
    pts = []
    for t, v in route:
        x = pad + ((t - t0) / tspan) * (w - pad * 2)
        y = h - pad - (v / vmax) * (h - pad * 2)
        pts.append((x, y))
    # Downsample to keep the path light.
    if len(pts) > 80:
        step = len(pts) / 80
        pts = [pts[int(i * step)] for i in range(80)] + [pts[-1]]
    return pts


def smooth_path(pts) -> str:
    d = f"M {pts[0][0]:.1f} {pts[0][1]:.1f}"
    n = len(pts)
    for i in range(n - 1):
        p0 = pts[max(i - 1, 0)]
        p1 = pts[i]
        p2 = pts[i + 1]
        p3 = pts[min(i + 2, n - 1)]
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        d += (
            f" C {c1[0]:.1f} {c1[1]:.1f}, {c2[0]:.1f} {c2[1]:.1f},"
            f" {p2[0]:.1f} {p2[1]:.1f}"
        )
    return d


def route_svg(route, w: int, h: int, stroke: float) -> str:
    pts = route_points(route, w, h, stroke * 1.5)
    path = smooth_path(pts)
    cx, cy = w / 2, h / 2

    # Expand the canvas so a rotated w×h path (plus round caps) is not clipped.
    rad = math.radians(abs(ROUTE_LINE_ROTATION))
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    rot_w = w * cos_a + h * sin_a
    rot_h = w * sin_a + h * cos_a
    pad_x = (rot_w - w) / 2 + stroke * 1.5
    pad_y = (rot_h - h) / 2 + stroke * 1.5
    vb_w = w + pad_x * 2
    vb_h = h + pad_y * 2

    return (
        f'<svg width="{vb_w:.0f}" height="{vb_h:.0f}" viewBox="0 0 {vb_w:.1f} {vb_h:.1f}">'
        f'<g transform="translate({pad_x:.1f} {pad_y:.1f}) '
        f'rotate({ROUTE_LINE_ROTATION} {cx} {cy})">'
        f'<path d="{path}" fill="none" stroke="{ACCENT_LINE}" '
        f'stroke-width="{stroke}" stroke-linecap="round" stroke-linejoin="round"/>'
        f"</g></svg>"
    )


# --------------------------------------------------------------------------
# HTML rendering
# --------------------------------------------------------------------------

def stat(value: str, label: str, *, label_first: bool = False) -> str:
    value_html = f'<div class="value">{value}</div>'
    label_html = f'<div class="label">{label}</div>'
    inner = (label_html + value_html) if label_first else (value_html + label_html)
    return f'<div class="stat">{inner}</div>'


HEAD = f"""<meta charset="utf-8">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@700&family=Open+Sans:wght@600;700;800&display=swap" rel="stylesheet">
<style>
  :root {{ --accent: {ACCENT}; --fg: #FFFFFF; --fg-dim: #FFFFFF; --bg: #0F0E0D; --card: #171514; --stroke: #2A2724; }}
  * {{ margin: 0; box-sizing: border-box; }}
  body {{ color: var(--fg); font-family: "Open Sans", sans-serif; }}
  /* Lift text/line off arbitrary photo backgrounds (Strava-style):
     a soft offset shadow for depth + a tight contact shadow for edge
     definition. Harmless on the dark preview page, essential on export. */
  .value, .label, .wordmark {{
    text-shadow: 0 1px 2px rgba(0, 0, 0, 0.18), 0 0 1px rgba(0, 0, 0, 0.20);
  }}
  .story svg, .strip-top svg {{ filter: drop-shadow(0 1px 2px rgba(0, 0, 0, 0.18)); }}
  .card {{
    background: var(--card); border: 1px solid var(--stroke); border-radius: 16px;
    display: flex; flex-direction: column; align-items: center;
  }}
  .story {{ width: 340px; padding: 32px 28px 36px; gap: 14px; }}
  .story-stats {{ display: flex; flex-direction: column; gap: 26px; align-items: center; width: 100%; }}
  .story .stat {{ align-items: center; gap: 7px; }}
  .story .label {{ font-size: 11px; letter-spacing: 0.06em; color: var(--fg); }}
  .story .value {{ font-size: 36px; }}
  .story .wordmark {{ font-size: 32px; }}
  .strip {{ width: 620px; flex-direction: column; align-items: stretch; gap: 8px; padding: 16px 24px 14px; border-radius: 12px; }}
  .strip-top {{ display: flex; align-items: center; gap: 20px; width: 100%; }}
  .strip-top .wordmark {{ flex-shrink: 0; font-size: 32px; }}
  .strip-top .route-wrap {{ flex: 1; display: flex; justify-content: center; min-width: 0; padding: 0; }}
  .story svg, .strip-top svg {{ display: block; }}
  .story svg {{ margin: 4px 0; }}
  .strip .stats-row {{ justify-content: center; margin-left: 0; width: 100%; }}
  .strip .stat {{ gap: 1px; }}
  .strip .value {{ font-size: 28px; }}
  .strip .label {{ font-size: 14px; }}
  .wordmark {{
    font-family: "Barlow Condensed", "Arial Narrow", sans-serif;
    font-size: 20px; font-weight: 700; letter-spacing: 0.06em;
    text-transform: uppercase;
  }}
  .stats-col {{ display: flex; flex-direction: column; gap: 26px; align-items: center; }}
  .stats-row {{ display: flex; gap: 36px; margin-left: auto; }}
  .stat {{ display: flex; flex-direction: column; gap: 0; white-space: nowrap; }}
  .value {{ font-size: 22px; font-weight: 700; line-height: 1; letter-spacing: -0.01em; font-variant-numeric: tabular-nums; text-align: center; }}
  .label {{ font-size: 16px; font-weight: 700; line-height: 1; letter-spacing: 0.12em; text-transform: uppercase; color: var(--fg); }}
</style>"""


def card_html(stats: dict, variant: str) -> str:
    lines = f"{stats['lines_added'] + stats['lines_removed']:,}"
    thinking = fmt_thinking(stats["api_ms"])
    tokens = fmt_tokens(stats["output_tokens"])
    stats_html = (
        stat(tokens, "Output Tokens", label_first=(variant == "story"))
        + stat(lines, "Lines Changed", label_first=(variant == "story"))
        + stat(thinking, "Thinking Time", label_first=(variant == "story"))
    )
    if variant == "story":
        return (
            '<div class="card story">'
            f'<div class="stats-col story-stats">{stats_html}</div>'
            f"{route_svg(stats['route'], 190, 78, 3.5)}"
            '<div class="wordmark">Claude</div></div>'
        )
    return (
        '<div class="card strip">'
        '<div class="strip-top">'
        '<div class="wordmark">Claude</div>'
        f'<div class="route-wrap">{route_svg(stats["route"], 220, 38, 1.5)}</div>'
        '</div>'
        f'<div class="stats-row">{stats_html}</div></div>'
    )


def render_html(stats: dict) -> str:
    meta = (
        f"{stats['project']} · {stats['prompts']} prompts · "
        f"{stats['tool_calls']} tool calls · elapsed {fmt_duration(stats['wall_ms'])}"
    )
    return f"""<!doctype html>
<html>
<head>
<title>Claude Overlay — {stats['project']}</title>
{HEAD}
<style>
  body {{
    background: var(--bg);
    min-height: 100vh; display: flex; flex-direction: column;
    align-items: center; gap: 48px; padding: 64px 24px;
  }}
  .hint {{ color: var(--fg-dim); font-size: 12px; letter-spacing: 0.08em; text-transform: uppercase; }}
  .meta {{ color: var(--fg-dim); font-size: 13px; }}
</style>
</head>
<body>
  <div class="hint">Story card</div>
  {card_html(stats, "story")}
  <div class="hint">Footer strip</div>
  {card_html(stats, "strip")}
  <div class="meta">{meta}</div>
</body>
</html>
"""


def render_export_html(stats: dict, variant: str) -> str:
    """A single card on a transparent page, for headless screenshotting.

    The card panel (background/border) is stripped so the PNG is fully
    transparent — just the wordmark, route line, and metrics, ready to
    overlay on a photo.
    """
    return f"""<!doctype html>
<html>
<head>{HEAD}
<style>
  html, body {{ background: transparent !important; }}
  body {{ width: 100vw; height: 100vh;
         display: flex; align-items: center; justify-content: center; }}
  .card, .strip, .strip-top, .route-wrap, .stats-row, .stat {{
    background: transparent !important; border: none !important;
  }}
</style>
</head>
<body>{card_html(stats, variant)}</body>
</html>
"""


# --------------------------------------------------------------------------
# PNG export via headless Chrome
# --------------------------------------------------------------------------

CHROME_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Arc.app/Contents/MacOS/Arc",
]

# Headless Chrome enforces a minimum window size on macOS, so small targets
# render off-center or blank. Screenshot on a roomy canvas (the card is
# flex-centered), then center-crop to the final frame with sips.
# window: CSS px · crop: device px (2x scale).
EXPORT_GEOMETRY = {
    "story": {"window": (600, 600), "crop": (760, 1000)},
    "strip": {"window": (800, 440), "trim_pad": 40},
}


def finalize_export_png(
    path: Path,
    *,
    crop: tuple[int, int] | None = None,
    trim_pad: int | None = None,
) -> None:
    """Center-crop and/or trim PNG while preserving transparency."""
    import subprocess

    try:
        from PIL import Image
    except ImportError:
        if crop:
            crop_w, crop_h = crop
            subprocess.run(
                ["sips", "--cropToHeightWidth", str(crop_h), str(crop_w), str(path)],
                check=True,
                capture_output=True,
            )
        elif trim_pad is not None:
            # Approximate center crop when Pillow is unavailable.
            subprocess.run(
                ["sips", "--cropToHeightWidth", "340", "1320", str(path)],
                check=True,
                capture_output=True,
            )
        return

    im = Image.open(path).convert("RGBA")
    if crop:
        crop_w, crop_h = crop
        left = max(0, (im.size[0] - crop_w) // 2)
        top = max(0, (im.size[1] - crop_h) // 2)
        im = im.crop((left, top, left + crop_w, top + crop_h))
    if trim_pad is not None:
        bbox = im.getbbox()
        if bbox:
            x0, y0, x1, y1 = bbox
            x0 = max(0, x0 - trim_pad)
            y0 = max(0, y0 - trim_pad)
            x1 = min(im.size[0], x1 + trim_pad)
            y1 = min(im.size[1], y1 + trim_pad)
            im = im.crop((x0, y0, x1, y1))
    px = im.load()
    for y in range(im.size[1]):
        for x in range(im.size[0]):
            if px[x, y][3] == 0:
                px[x, y] = (0, 0, 0, 0)
    im.save(path, format="PNG", optimize=True)


def find_chrome() -> str | None:
    for p in CHROME_PATHS:
        if Path(p).is_file():
            return p
    return None


def export_pngs(stats: dict, *, variants: list[str] | None = None) -> list[Path]:
    import subprocess
    import tempfile

    chrome = find_chrome()
    if not chrome:
        print(
            "No Chromium-based browser found for PNG export — "
            "open the HTML and screenshot instead.",
            file=sys.stderr,
        )
        return []

    targets = variants or list(EXPORT_GEOMETRY)
    outputs = []
    for variant in targets:
        geo = EXPORT_GEOMETRY[variant]
        w, h = geo["window"]
        with tempfile.NamedTemporaryFile(
            suffix=".html", delete=False, mode="w"
        ) as f:
            f.write(render_export_html(stats, variant))
            tmp = Path(f.name)
        png = OUT_DIR / f"overlay-{stats['session_id'][:8]}-{variant}.png"
        subprocess.run(
            [
                chrome,
                "--headless=new",
                f"--screenshot={png}",
                f"--window-size={w},{h}",
                "--force-device-scale-factor=2",
                "--default-background-color=00000000",
                "--hide-scrollbars",
                "--virtual-time-budget=5000",
                tmp.as_uri(),
            ],
            check=True,
            capture_output=True,
        )
        tmp.unlink()
        finalize_export_png(
            png,
            crop=geo.get("crop"),
            trim_pad=geo.get("trim_pad"),
        )
        outputs.append(png)
    return outputs


# --------------------------------------------------------------------------
# Local share server (QR links to your card over Wi-Fi — no backend needed)
# --------------------------------------------------------------------------

SHARE_TTL_SECS = 300  # server self-terminates after 5 minutes


def lan_ip() -> str:
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No packets are sent; this just selects the outbound interface.
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def share_page_html(sid8: str, variants: list[str]) -> str:
    ordered: list[str] = []
    for v in ("story", "strip"):
        if v in variants:
            ordered.append(v)
    for v in variants:
        if v not in ordered:
            ordered.append(v)

    labels = {"story": "Story card", "strip": "Footer strip"}
    slides = "\n".join(
        f'<div class="slide" data-variant="{v}">'
        f'<img src="/overlay-{sid8}-{v}.png" alt="{labels.get(v, v)}" '
        f'draggable="false"></div>'
        for v in ordered
    )
    dots = ""
    dots_html = ""
    if len(ordered) > 1:
        dots = "\n".join(
            f'<button type="button" class="dot{" active" if i == 0 else ""}" '
            f'data-index="{i}" aria-label="{labels.get(v, v)}"></button>'
            for i, v in enumerate(ordered)
        )
        dots_html = f'<div class="dots" id="dots">{dots}</div>'

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Claude Overlay — session {sid8}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; }}
  html, body {{ height: 100%; }}
  body {{
    background: #141210; color: #FFFFFF;
    font: 14px/1.5 -apple-system, system-ui, sans-serif;
    min-height: 100dvh; display: flex; flex-direction: column;
  }}
  .page {{
    flex: 1; display: flex; flex-direction: column;
    min-height: 100dvh;
    padding: max(16px, env(safe-area-inset-top)) 16px 0;
  }}
  .main {{
    flex: 1; display: flex; align-items: center; justify-content: center;
    min-height: 0; width: 100%; padding: 0 0 16px;
  }}
  .viewport {{
    width: 100%; overflow: hidden; touch-action: pan-y pinch-zoom;
  }}
  .track {{
    display: flex; align-items: center;
    transition: transform 0.28s ease;
    will-change: transform;
  }}
  .slide {{
    flex: 0 0 100%;
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    padding: 0 4px;
  }}
  .slide img {{
    display: block; width: 100%; height: auto;
    max-width: min(92vw, 420px);
    -webkit-touch-callout: default;
    user-select: none; -webkit-user-select: none;
  }}
  .slide[data-variant="strip"] img {{ max-width: min(92vw, 620px); }}
  .footer {{
    flex-shrink: 0; display: flex; flex-direction: column; align-items: center;
    gap: 16px; padding: 0 16px calc(20px + env(safe-area-inset-bottom));
  }}
  .dots {{
    display: flex; justify-content: center; gap: 8px;
  }}
  .dot {{
    width: 7px; height: 7px; border: 0; border-radius: 50%;
    background: #3A3632; padding: 0; cursor: pointer;
  }}
  .dot.active {{ background: #EA9A76; }}
  .hint {{
    text-align: center; font-size: 13px; color: #FFFFFF;
  }}
  .note {{
    text-align: center; font-size: 12px; line-height: 1.45;
    color: #B8B0A8; max-width: 320px;
  }}
  .note.wink {{ color: #8A827A; font-style: italic; }}
</style>
</head><body>
<div class="page">
  <div class="main">
    <div class="viewport" id="viewport">
      <div class="track" id="track">{slides}</div>
    </div>
  </div>
  <div class="footer">
    {dots_html}
    <p class="hint">Press and hold the image, then tap Save to Photos.</p>
    <p class="note">These stats add up your Claude Code sessions from today. Cutoff at 4 AM instead of 12 AM to account for peak developer time.</p>
  </div>
</div>
<script>
(function () {{
  const track = document.getElementById("track");
  const dots = document.querySelectorAll(".dot");
  let index = 0;
  let startX = 0;

  function goTo(i) {{
    index = Math.max(0, Math.min(dots.length - 1, i));
    track.style.transform = "translateX(-" + (index * 100) + "%)";
    dots.forEach((d, j) => d.classList.toggle("active", j === index));
  }}

  dots.forEach((d) => d.addEventListener("click", () => goTo(+d.dataset.index)));

  const viewport = document.getElementById("viewport");
  viewport.addEventListener("touchstart", (e) => {{
    startX = e.touches[0].clientX;
  }}, {{ passive: true }});
  viewport.addEventListener("touchend", (e) => {{
    const dx = e.changedTouches[0].clientX - startX;
    if (dx < -40) goTo(index + 1);
    else if (dx > 40) goTo(index - 1);
  }}, {{ passive: true }});

  goTo(0);
}})();
</script>
</body></html>"""


def run_share_server(
    sid8: str, port: int, variants: list[str] | None = None,
) -> int:
    """Internal mode: serve the share page + PNGs from OUT_DIR, then exit."""
    import http.server
    import os
    import threading

    if variants is None:
        variants = [
            v for v in EXPORT_GEOMETRY
            if (OUT_DIR / f"overlay-{sid8}-{v}.png").is_file()
        ]
    page = share_page_html(sid8, variants).encode()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(OUT_DIR), **kw)

        def do_GET(self):
            if self.path in ("/", f"/s/{sid8}"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
            elif self.path.endswith(".png"):
                file_path = OUT_DIR / Path(self.path.lstrip("/")).name
                if file_path.is_file():
                    data = file_path.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self.send_error(404)
            else:
                super().do_GET()

        def log_message(self, *a):
            pass

    threading.Timer(SHARE_TTL_SECS, lambda: os._exit(0)).start()
    with http.server.ThreadingHTTPServer(("", port), Handler) as srv:
        srv.serve_forever()
    return 0


def start_share_server(
    sid8: str, variants: list[str] | None = None,
) -> str | None:
    """Spawn a detached copy of this script in serve mode; return the URL."""
    import socket
    import subprocess

    with socket.socket() as s:
        s.bind(("", 0))
        port = s.getsockname()[1]

    cmd = [
        sys.executable, str(Path(__file__).resolve()),
        "--serve", sid8, "--serve-port", str(port),
    ]
    if variants:
        cmd.extend(["--serve-variants", ",".join(variants)])
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return f"http://{lan_ip()}:{port}/s/{sid8}"


# --------------------------------------------------------------------------
# Terminal QR code (for scanning the share link from a phone)
# --------------------------------------------------------------------------

def print_qr(url: str) -> None:
    try:
        import qrcode
    except ImportError:
        print(
            "QR skipped — install with `pip3 install qrcode` to render "
            "scannable share codes in the terminal.",
            file=sys.stderr,
        )
        return
    qr = qrcode.QRCode(
        border=2, error_correction=qrcode.constants.ERROR_CORRECT_L
    )
    qr.add_data(url)
    qr.make(fit=True)
    print()
    qr.print_ascii(invert=True)
    print(f"  Scan to view & share: {url}\n")


# --------------------------------------------------------------------------
# Rate limit detection (from statusline snapshots — zero tokens)
# --------------------------------------------------------------------------

def latest_statusline_snapshot() -> dict | None:
    if not SNAPSHOT_DIR.is_dir():
        return None
    files = sorted(
        SNAPSHOT_DIR.glob("*.ndjson"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in files:
        try:
            lines = path.read_text().splitlines()
            if not lines:
                continue
            return json.loads(lines[-1])
        except (json.JSONDecodeError, OSError, IndexError):
            continue
    return None


def rate_limit_status() -> tuple[bool, list[str]]:
    snap = latest_statusline_snapshot()
    if not snap:
        return False, []
    limits = snap.get("rate_limits") or {}
    hit = []
    for name, info in limits.items():
        if isinstance(info, dict) and info.get("used_percentage", 0) >= 100:
            hit.append(name.replace("_", " "))
    return bool(hit), hit


def format_rate_reset() -> str:
    snap = latest_statusline_snapshot()
    if not snap:
        return ""
    limits = snap.get("rate_limits") or {}
    resets = [
        info["resets_at"]
        for info in limits.values()
        if isinstance(info, dict) and info.get("resets_at")
    ]
    if not resets:
        return ""
    ts = min(resets)
    return datetime.fromtimestamp(ts).strftime("%-I:%M %p %Z").strip()


def overlay_cli_command() -> str:
    return f'python3 "{Path(__file__).resolve()}" --export --qr'


def print_rate_status() -> int:
    limited, windows = rate_limit_status()
    if not limited:
        print("OK: session usage limit not reached — /overlay should work.")
        return 0

    reset = format_rate_reset()
    reset_line = f" Resets {reset}." if reset else ""
    print(
        "LIMIT REACHED: session usage limit hit"
        f" ({', '.join(windows)}).{reset_line}\n"
        "/overlay should still work — a hook runs the generator locally.\n"
        "If it doesn't, run this instead (zero tokens):\n"
        f"  {overlay_cli_command()}\n\n"
        "Or in Claude Code bash mode:\n"
        f"  !{overlay_cli_command()}"
    )
    return 1


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", help="session id (transcript filename stem)")
    ap.add_argument(
        "--no-fallback",
        action="store_true",
        help="use the exact session only (no sibling fallback)",
    )
    ap.add_argument(
        "--transcript-path",
        help="exact transcript JSONL path (from Claude Code hook payload)",
    )
    ap.add_argument("--list", action="store_true", help="list recent sessions")
    ap.add_argument("--no-open", action="store_true", help="don't open browser")
    ap.add_argument(
        "--export", action="store_true", help="also export PNGs (story + strip)"
    )
    ap.add_argument(
        "--qr",
        action="store_true",
        help="print a QR code that opens the card on your phone (same Wi-Fi)",
    )
    ap.add_argument(
        "--share-url",
        help="custom URL to encode in the QR (skips the local share server)",
    )
    ap.add_argument("--serve", help=argparse.SUPPRESS)
    ap.add_argument("--serve-port", type=int, help=argparse.SUPPRESS)
    ap.add_argument("--serve-variants", help=argparse.SUPPRESS)
    ap.add_argument(
        "--rate-status",
        action="store_true",
        help="check session usage limit and print fallback command if hit",
    )
    ap.add_argument("--quiet-if-empty", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.serve:
        serve_variants = None
        if args.serve_variants:
            serve_variants = [
                v.strip() for v in args.serve_variants.split(",") if v.strip()
            ]
        return run_share_server(args.serve, args.serve_port, serve_variants)

    if args.rate_status:
        return print_rate_status()

    transcripts = find_transcripts()
    # Nothing to evaluate on — do nothing, quietly.
    if not transcripts:
        return 0

    if args.list:
        for p in transcripts[:15]:
            mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%b %d %H:%M")
            print(f"{p.stem}  {mtime}  {p.parent.name}")
        return 0

    if args.session:
        # Single-session view, for inspecting one specific session.
        path, _ = resolve_transcript(
            transcripts, session_id=args.session, allow_fallback=False
        )
        if not path:
            print(f"No transcript matching {args.session!r}", file=sys.stderr)
            return 1
        stats = apply_snapshot(parse_transcript(path))
    else:
        # Default: aggregate every session in the current 4am-to-4am day.
        stats = parse_day(transcripts)

    # No activity in the window — nothing to show, so do nothing.
    if session_is_empty(stats):
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"overlay-{stats['session_id'][:8]}.html"
    out.write_text(render_html(stats))

    if args.session:
        print(f"Session:  {stats['session_id']}")
        print(f"Project:  {stats['project']}")
    else:
        sess = stats["sessions"]
        proj = stats["project_count"]
        print(
            f"Day:      {stats['project']} · {sess} session{'s' if sess != 1 else ''}"
            f" across {proj} project{'s' if proj != 1 else ''} (since 4am)"
        )
    print(
        f"Stats:    +{stats['lines_added']}/-{stats['lines_removed']} lines · "
        f"{fmt_tokens(stats['output_tokens'])} output tokens · "
        f"thinking {fmt_duration(stats['api_ms'])} · "
        f"elapsed {fmt_duration(stats['wall_ms'])}"
    )

    sid8 = stats["session_id"][:8]
    exported = []
    if args.export or args.qr:
        export_variants = list(EXPORT_GEOMETRY) if args.export else ["story"]
        exported = export_pngs(stats, variants=export_variants)
        print_variants = export_variants if args.export else ["story"]
        for variant, label in (("story", "Story"), ("strip", "Footer")):
            if variant not in print_variants:
                continue
            path = OUT_DIR / f"overlay-{sid8}-{variant}.png"
            if path in exported:
                print(f"{label + ':':<8} {path}")
    elif not args.no_open:
        print(f"Overlay:  {out}")

    if args.qr:
        if args.share_url:
            print_qr(args.share_url)
        elif exported:
            url = start_share_server(sid8, variants=["story"])
            print_qr(url)
            print(
                f"  Link serves your card on this Wi-Fi network for "
                f"{SHARE_TTL_SECS // 60} minutes."
            )
        else:
            print("QR skipped — PNG export failed.", file=sys.stderr)

    if not args.no_open and not args.qr:
        webbrowser.open(out.as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
from datetime import datetime, timezone
from pathlib import Path

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
SNAPSHOT_DIR = Path.home() / ".claude-overlay" / "sessions"
OUT_DIR = Path(__file__).parent / "out"

ACCENT = "#D97757"  # Claude terracotta
ACCENT_LINE = "#EA9A76"  # slightly brighter for the route line


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


def parse_transcript(path: Path) -> dict:
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
    pts = route_points(route, w, h, stroke * 2.5)
    return (
        f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
        f'<path d="{smooth_path(pts)}" fill="none" stroke="{ACCENT_LINE}" '
        f'stroke-width="{stroke}" stroke-linecap="round" stroke-linejoin="round"/>'
        "</svg>"
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
<link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@700&family=Open+Sans:wght@500;600&display=swap" rel="stylesheet">
<style>
  :root {{ --accent: {ACCENT}; --fg: #E8E6E3; --fg-dim: #8A8782; --bg: #0F0E0D; --card: #171514; --stroke: #2A2724; }}
  * {{ margin: 0; box-sizing: border-box; }}
  body {{ color: var(--fg); font-family: "Open Sans", sans-serif; }}
  .card {{
    background: var(--card); border: 1px solid var(--stroke); border-radius: 16px;
    display: flex; flex-direction: column; align-items: center;
  }}
  .story {{ width: 340px; padding: 32px 28px 36px; gap: 24px; }}
  .story-stats {{ display: flex; flex-direction: column; gap: 20px; align-items: center; width: 100%; }}
  .story .stat {{ align-items: center; gap: 4px; }}
  .story .label {{ font-size: 8px; color: var(--fg); }}
  .story .value {{ font-size: 24px; }}
  .story .wordmark {{ margin-top: 4px; }}
  .strip {{ width: 620px; flex-direction: row; align-items: center; gap: 20px; padding: 14px 22px; border-radius: 12px; }}
  .strip svg {{ flex-shrink: 0; margin-left: 12px; margin-right: 10px; }}
  .strip .value {{ font-size: 17px; }}
  .strip .label {{ font-size: 9px; }}
  .wordmark {{
    font-family: "Barlow Condensed", "Arial Narrow", sans-serif;
    font-size: 20px; font-weight: 700; letter-spacing: 0.22em;
    text-transform: uppercase;
  }}
  .stats-col {{ display: flex; flex-direction: column; gap: 20px; align-items: center; }}
  .stats-row {{ display: flex; gap: 28px; margin-left: auto; }}
  .stat {{ display: flex; flex-direction: column; gap: 2px; white-space: nowrap; }}
  .value {{ font-size: 24px; font-weight: 600; letter-spacing: -0.01em; font-variant-numeric: tabular-nums; }}
  .label {{ font-size: 10px; font-weight: 500; letter-spacing: 0.12em; text-transform: uppercase; color: var(--fg); }}
</style>"""


def card_html(stats: dict, variant: str) -> str:
    lines = f"+{stats['lines_added']:,}"
    thinking = fmt_duration(stats["api_ms"])
    tokens = fmt_tokens(stats["output_tokens"])
    stats_html = (
        stat(lines, "Lines added", label_first=(variant == "story"))
        + stat(thinking, "Thinking time", label_first=(variant == "story"))
        + stat(tokens, "Output tokens", label_first=(variant == "story"))
    )
    if variant == "story":
        return (
            '<div class="card story">'
            f'<div class="stats-col story-stats">{stats_html}</div>'
            f"{route_svg(stats['route'], 230, 120, 3)}"
            '<div class="wordmark">Claude</div></div>'
        )
    return (
        '<div class="card strip">'
        '<div class="wordmark" style="margin-right:-8px">Claude</div>'
        f"{route_svg(stats['route'], 130, 40, 2)}"
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
  body {{ background: transparent; width: 100vw; height: 100vh;
         display: flex; align-items: center; justify-content: center; }}
  .card {{ background: transparent; border: none; }}
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
    "strip": {"window": (800, 300), "crop": (1320, 200)},
}


def find_chrome() -> str | None:
    for p in CHROME_PATHS:
        if Path(p).is_file():
            return p
    return None


def export_pngs(stats: dict) -> list[Path]:
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

    outputs = []
    for variant, geo in EXPORT_GEOMETRY.items():
        w, h = geo["window"]
        crop_w, crop_h = geo["crop"]
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
        subprocess.run(
            ["sips", "--cropToHeightWidth", str(crop_h), str(crop_w), str(png)],
            check=True,
            capture_output=True,
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
        f'<div class="preview-card preview-card--{v}">'
        f'<a class="preview-save" href="/overlay-{sid8}-{v}.png">'
        f'<img src="/overlay-{sid8}-{v}.png" alt="{labels.get(v, v)}" '
        f'draggable="false"></a></div></div>'
        for v in ordered
    )
    dots = "\n".join(
        f'<button type="button" class="dot{" active" if i == 0 else ""}" '
        f'data-index="{i}" aria-label="{labels.get(v, v)}"></button>'
        for i, v in enumerate(ordered)
    )

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Claude Overlay — session {sid8}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; }}
  body {{
    background: #141210; color: #E8E6E3;
    font: 14px/1.5 -apple-system, system-ui, sans-serif;
    min-height: 100dvh; display: flex; flex-direction: column;
  }}
  .page {{
    flex: 1; display: flex; flex-direction: column;
    padding: 24px 16px calc(20px + env(safe-area-inset-bottom));
  }}
  .carousel {{
    flex: 1; display: flex; flex-direction: column;
    gap: 16px; min-height: 0;
  }}
  .viewport {{
    flex: 1; min-height: 0;
    overflow: hidden; width: 100%; touch-action: pan-y pinch-zoom;
  }}
  .track {{
    display: flex; height: 100%; align-items: stretch;
    transition: transform 0.28s ease;
    will-change: transform;
  }}
  .slide {{
    flex: 0 0 100%; height: 100%;
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    gap: 12px; padding: 0 4px;
  }}
  .preview-card {{
    width: 100%; max-width: min(92vw, 420px);
    border: 1px solid #2A2724; border-radius: 16px;
    padding: 24px 20px; background: transparent;
  }}
  .preview-card--strip {{
    max-width: min(92vw, 620px); border-radius: 12px;
    padding: 16px 18px;
  }}
  .preview-save {{
    display: block; position: relative; border-radius: 8px;
    overflow: hidden; -webkit-touch-callout: default;
  }}
  .preview-save::before {{
    content: ""; position: absolute; inset: 0; z-index: 0;
    background-color: #171514;
    background-image:
      linear-gradient(45deg, #24211e 25%, transparent 25%),
      linear-gradient(-45deg, #24211e 25%, transparent 25%),
      linear-gradient(45deg, transparent 75%, #24211e 75%),
      linear-gradient(-45deg, transparent 75%, #24211e 75%);
    background-size: 14px 14px;
    background-position: 0 0, 0 7px, 7px -7px, -7px 0;
  }}
  .preview-save img {{
    position: relative; z-index: 1;
    width: 100%; height: auto; display: block;
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
    text-align: center; padding-top: 20px;
    font-size: 13px; color: #8A8782;
  }}
</style>
</head><body>
<div class="page">
  <div class="carousel">
    <div class="viewport" id="viewport">
      <div class="track" id="track">{slides}</div>
    </div>
    <div class="dots" id="dots">{dots}</div>
  </div>
  <p class="hint">Press and hold the image to save the transparent PNG.</p>
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


def run_share_server(sid8: str, port: int) -> int:
    """Internal mode: serve the share page + PNGs from OUT_DIR, then exit."""
    import http.server
    import os
    import threading

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
            else:
                super().do_GET()

        def log_message(self, *a):
            pass

    threading.Timer(SHARE_TTL_SECS, lambda: os._exit(0)).start()
    with http.server.ThreadingHTTPServer(("", port), Handler) as srv:
        srv.serve_forever()
    return 0


def start_share_server(sid8: str) -> str | None:
    """Spawn a detached copy of this script in serve mode; return the URL."""
    import socket
    import subprocess

    with socket.socket() as s:
        s.bind(("", 0))
        port = s.getsockname()[1]

    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()),
         "--serve", sid8, "--serve-port", str(port)],
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
    ap.add_argument(
        "--rate-status",
        action="store_true",
        help="check session usage limit and print fallback command if hit",
    )
    ap.add_argument("--quiet-if-empty", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.serve:
        return run_share_server(args.serve, args.serve_port)

    if args.rate_status:
        return print_rate_status()

    transcripts = find_transcripts()
    if not transcripts:
        print("No Claude transcripts found in ~/.claude/projects", file=sys.stderr)
        return 1

    if args.list:
        for p in transcripts[:15]:
            mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%b %d %H:%M")
            print(f"{p.stem}  {mtime}  {p.parent.name}")
        return 0

    if args.session:
        path, fallback_from = resolve_transcript(
            transcripts, session_id=args.session, allow_fallback=False
        )
        if not path:
            print(f"No transcript matching {args.session!r}", file=sys.stderr)
            return 1
    else:
        path, fallback_from = resolve_transcript(
            transcripts,
            session_id=None,
            transcript_path=args.transcript_path,
            allow_fallback=not args.no_fallback,
        )
        if not path:
            print("No Claude transcripts found in ~/.claude/projects", file=sys.stderr)
            return 1

    stats = apply_snapshot(parse_transcript(path))

    if args.quiet_if_empty and session_is_empty(stats):
        return 0

    if fallback_from:
        print(
            f"Note:     using {path.stem[:8]} — "
            f"{fallback_from.stem[:8]} has no output yet "
            f"(rate limit or new session)",
            file=sys.stderr,
        )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"overlay-{stats['session_id'][:8]}.html"
    out.write_text(render_html(stats))

    print(f"Session:  {stats['session_id']}")
    print(f"Project:  {stats['project']}")
    print(
        f"Stats:    +{stats['lines_added']}/-{stats['lines_removed']} lines · "
        f"{fmt_tokens(stats['output_tokens'])} output tokens · "
        f"thinking {fmt_duration(stats['api_ms'])} · "
        f"elapsed {fmt_duration(stats['wall_ms'])}"
    )

    sid8 = stats["session_id"][:8]
    exported = []
    if args.export or args.qr:
        exported = export_pngs(stats)
        story = OUT_DIR / f"overlay-{sid8}-story.png"
        if story in exported:
            print(f"Story:    {story}")
    elif not args.no_open:
        print(f"Overlay:  {out}")

    if args.qr:
        if args.share_url:
            print_qr(args.share_url)
        elif exported:
            url = start_share_server(sid8)
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

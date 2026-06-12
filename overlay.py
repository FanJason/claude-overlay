#!/usr/bin/env python3
"""Generate a Strava-style overlay card for a Claude Code session.

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

ACCENT = "#D97757"  # Claude terracotta — our Strava orange


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
        f'<path d="{smooth_path(pts)}" fill="none" stroke="{ACCENT}" '
        f'stroke-width="{stroke}" stroke-linecap="round" stroke-linejoin="round"/>'
        "</svg>"
    )


# --------------------------------------------------------------------------
# HTML rendering
# --------------------------------------------------------------------------

def stat(value: str, label: str) -> str:
    return (
        '<div class="stat"><div class="value">'
        f"{value}</div><div class=\"label\">{label}</div></div>"
    )


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
  .story {{ width: 340px; padding: 32px 28px 36px; gap: 28px; }}
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
  .story .stat {{ align-items: center; }}
  .value {{ font-size: 24px; font-weight: 600; letter-spacing: -0.01em; font-variant-numeric: tabular-nums; }}
  .label {{ font-size: 10px; font-weight: 500; letter-spacing: 0.12em; text-transform: uppercase; color: var(--fg); }}
</style>"""


def card_html(stats: dict, variant: str) -> str:
    lines = f"+{stats['lines_added']:,}"
    thinking = fmt_duration(stats["api_ms"])
    tokens = fmt_tokens(stats["output_tokens"])
    stats_html = (
        stat(lines, "Lines added")
        + stat(thinking, "Thinking time")
        + stat(tokens, "Output tokens")
    )
    if variant == "story":
        return (
            '<div class="card story"><div class="wordmark">Claude</div>'
            f"{route_svg(stats['route'], 230, 120, 3)}"
            f'<div class="stats-col">{stats_html}</div></div>'
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
    """A single card on a transparent page, for headless screenshotting."""
    return f"""<!doctype html>
<html>
<head>{HEAD}
<style>
  body {{ background: transparent; width: 100vw; height: 100vh;
         display: flex; align-items: center; justify-content: center; }}
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
# CLI
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", help="session id (transcript filename stem)")
    ap.add_argument("--list", action="store_true", help="list recent sessions")
    ap.add_argument("--no-open", action="store_true", help="don't open browser")
    ap.add_argument(
        "--export", action="store_true", help="also export PNGs (story + strip)"
    )
    ap.add_argument(
        "--qr",
        action="store_true",
        help="print a scannable QR code for the share link",
    )
    ap.add_argument(
        "--share-url",
        help="share URL to encode in the QR (defaults to a placeholder)",
    )
    args = ap.parse_args()

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
        matches = [p for p in transcripts if p.stem.startswith(args.session)]
        if not matches:
            print(f"No transcript matching {args.session!r}", file=sys.stderr)
            return 1
        path = matches[0]
    else:
        path = transcripts[0]

    stats = apply_snapshot(parse_transcript(path))

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
    print(f"Overlay:  {out}")

    if args.export:
        for png in export_pngs(stats):
            print(f"PNG:      {png}")

    if args.qr:
        # Placeholder until the hosted share page exists; --share-url overrides.
        url = args.share_url or (
            f"https://claude-overlay.app/s/{stats['session_id'][:8]}"
        )
        print_qr(url)

    if not args.no_open:
        webbrowser.open(out.as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())

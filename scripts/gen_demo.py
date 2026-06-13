#!/usr/bin/env python3
"""Regenerate public/demo/story.png from the current card layout.

The demo is a curated "a day of Claude Code" story card. We feed a
representative stats dict (a believable cumulative-token route plus the
headline metrics) through the same export path /overlay uses, so the demo
always matches what the plugin actually renders.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import overlay  # noqa: E402

# Cumulative output tokens over a working day (always increasing), ending at
# the headline 264.8K. Timestamps are arbitrary monotonic ticks.
ROUTE_VALUES = [
    1_800, 9_400, 14_200, 28_500, 41_000, 52_300, 78_600, 96_400,
    119_000, 138_500, 162_000, 181_700, 205_300, 229_800, 250_100, 264_800,
]
route = [(float(i), v) for i, v in enumerate(ROUTE_VALUES)]

stats = {
    "session_id": "demoday0",
    "lines_added": 2_106,
    "lines_removed": 1_078,        # combined -> 3,184 Lines Changed
    "output_tokens": 264_800,      # -> 264.8K
    "api_ms": 4_720_000,           # 1h 18m 40s -> "1h 18m"
    "route": route,
}

out = overlay.export_pngs(stats, variants=["story"])
if not out:
    sys.exit("export failed (no browser?)")

dest = ROOT / "public" / "demo" / "story.png"
src = out[0]
dest.write_bytes(src.read_bytes())
print(f"wrote {dest} from {src}")

"""Draw the application icon. Development only; the ico is committed for builds.

Palette follows the web UI: warm paper tile with an ink ring (Kanso-inspired
paper/ink pair), so the taskbar, shortcut and window icon match the app.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

PAPER = (239, 233, 221, 255)
INK = (42, 41, 38, 255)
HAIRLINE = (42, 41, 38, 46)


def draw(size: int) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    pen = ImageDraw.Draw(image)
    pad = size * 0.08
    radius = size * 0.24
    box = (pad, pad, size - pad, size - pad)
    pen.rounded_rectangle(box, radius=radius, fill=PAPER)
    pen.rounded_rectangle(box, radius=radius, outline=HAIRLINE, width=max(1, round(size * 0.012)))

    cx = cy = size / 2
    outer = size * 0.35
    inner = size * 0.21
    pen.ellipse((cx - outer, cy - outer, cx + outer, cy + outer), fill=INK)
    pen.ellipse((cx - inner, cy - inner, cx + inner, cy + inner), fill=PAPER)
    return image


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    out = root / "assets" / "icon.ico"
    out.parent.mkdir(parents=True, exist_ok=True)
    sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    draw(256).save(out, format="ICO", sizes=sizes)
    print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()

"""Draw the application icon. Development only; the ico is committed for builds."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


def draw(size: int) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    pen = ImageDraw.Draw(image)
    pad = size * 0.08
    radius = size * 0.22
    pen.rounded_rectangle((pad, pad, size - pad, size - pad), radius=radius, fill=(37, 99, 235, 255))
    cx = cy = size / 2
    outer = size * 0.28
    inner = size * 0.14
    pen.ellipse((cx - outer, cy - outer, cx + outer, cy + outer), fill=(255, 255, 255, 255))
    pen.ellipse((cx - inner, cy - inner, cx + inner, cy + inner), fill=(37, 99, 235, 255))
    return image


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    out = root / "assets" / "icon.ico"
    out.parent.mkdir(parents=True, exist_ok=True)
    sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    draw(256).save(out, format="ICO", sizes=sizes)


if __name__ == "__main__":
    main()

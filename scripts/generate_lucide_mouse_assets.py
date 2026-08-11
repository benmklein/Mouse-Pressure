"""Generate application and Krita PNG/ICO assets from the Lucide Mouse glyph."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
APP_ASSETS = ROOT / "src" / "mouse_pressure" / "assets"
KRITA_ASSETS = ROOT / "integrations" / "krita" / "mouse_pressure_brush"


def _rounded_line(draw: ImageDraw.ImageDraw, points, *, fill, width: int) -> None:
    draw.line(points, fill=fill, width=width, joint="curve")
    radius = width // 2
    for x, y in (points[0], points[-1]):
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=fill)


def _mouse_glyph(size: int, color: tuple[int, int, int, int]) -> Image.Image:
    scale = 8
    canvas_size = size * scale
    image = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    width = max(scale * 2, round(canvas_size * 2 / 24))
    margin_x = round(canvas_size * 5 / 24)
    margin_y = round(canvas_size * 2 / 24)
    draw.rounded_rectangle(
        (margin_x, margin_y, canvas_size - margin_x, canvas_size - margin_y),
        radius=round(canvas_size * 7 / 24),
        outline=color,
        width=width,
    )
    _rounded_line(
        draw,
        [
            (canvas_size // 2, round(canvas_size * 6 / 24)),
            (canvas_size // 2, round(canvas_size * 10 / 24)),
        ],
        fill=color,
        width=width,
    )
    return image.resize((size, size), Image.Resampling.LANCZOS)


def _app_icon(size: int) -> Image.Image:
    scale = 4
    canvas_size = size * scale
    image = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    inset = round(canvas_size * 0.04)
    draw.rounded_rectangle(
        (inset, inset, canvas_size - inset, canvas_size - inset),
        radius=round(canvas_size * 0.22),
        fill=(15, 118, 110, 255),
    )
    glyph = _mouse_glyph(round(canvas_size * 0.68), (255, 255, 255, 255))
    image.alpha_composite(
        glyph,
        ((canvas_size - glyph.width) // 2, (canvas_size - glyph.height) // 2),
    )
    return image.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    APP_ASSETS.mkdir(parents=True, exist_ok=True)
    KRITA_ASSETS.mkdir(parents=True, exist_ok=True)

    app_png = _app_icon(256)
    app_png.save(APP_ASSETS / "lucide_mouse.png")
    app_png.save(
        APP_ASSETS / "lucide_mouse.ico",
        sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )

    _mouse_glyph(22, (32, 38, 44, 255)).save(KRITA_ASSETS / "mouse_pressure_mouse.png")
    _mouse_glyph(22, (32, 38, 44, 255)).save(
        KRITA_ASSETS / "dark_mouse_pressure_mouse.png"
    )
    _mouse_glyph(22, (242, 246, 250, 255)).save(
        KRITA_ASSETS / "light_mouse_pressure_mouse.png"
    )


if __name__ == "__main__":
    main()

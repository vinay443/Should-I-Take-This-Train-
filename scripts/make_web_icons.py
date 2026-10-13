"""Draw the web app's home-screen icons (src/sitt/web/static/icons/).

    uv run python scripts/make_web_icons.py

Three amber indicator dots on the board's dark background, the same as the page's favicon.
The background fills the whole square and the dots sit well inside the central 80%, so
the same picture works as a "maskable" icon (Android crops it to a circle or squircle).
The PNGs are committed; re-run this only to change the design.
"""

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parents[1] / "src" / "sitt" / "web" / "static" / "icons"
BACKGROUND = (22, 25, 29)  # #16191d, the board
AMBER = (255, 176, 32)  # #ffb020, the board's lamps
SIZES = (192, 512)


def draw(size: int) -> Image.Image:
    scale = 4  # draw large, then shrink, for smooth edges
    big = size * scale
    image = Image.new("RGB", (big, big), BACKGROUND)
    pen = ImageDraw.Draw(image)
    radius = big * 0.085
    for x in (0.31, 0.5, 0.69):
        cx, cy = big * x, big * 0.5
        pen.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=AMBER)
    return image.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        path = OUT / f"icon-{size}.png"
        draw(size).save(path, optimize=True)
        print(f"wrote {path} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()

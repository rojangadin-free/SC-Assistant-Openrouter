"""
make_pwa_icons.py — generate the installed-app icons from the school logo.

    python tools/make_pwa_icons.py

Run once (and again whenever logo.png changes). The output is committed, so the
app never depends on Pillow at runtime.

Why generated rather than hand-made
-----------------------------------
`static/images/logo.png` is 111x110. A manifest that points at it produces an
install prompt Chrome refuses ("no icon of at least 144px") and, on Android, a
home-screen icon upscaled by the launcher into a blurry square. The two sizes
below are the ones the platforms actually require:

  192  Android home screen / the install prompt
  512  splash screen while the app boots, and the Play-style listing

Maskable, and why the padding is 20%
------------------------------------
Android crops every icon to whatever shape the launcher uses — circle, squircle,
teardrop. A logo drawn edge-to-edge loses its rim to that crop. The maskable spec
guarantees only the middle 80% survives, so the logo is drawn into the centre
with 20% padding on every side and the school green fills the rest. That also
removes the transparency: an RGBA icon on a dark launcher renders as a green
smear with invisible text.
"""

# Make the repo root importable and force the CWD there: this script lives in
# tools/ but every path and import below assumes the repo root. Must come
# before the first repo import.
import _bootstrap  # noqa: F401

import os

from PIL import Image

SRC = os.path.join("sc_assistant", "static", "images", "logo.png")
OUT_DIR = os.path.join("sc_assistant", "static", "images")

# The school green, matching --sc-green in main.css. The icon must not be white:
# it sits on a light launcher background half the time.
BACKGROUND = (13, 69, 3, 255)

# The safe zone for a maskable icon is the middle 80%; anything outside it is a
# bleed area the launcher is free to cut off.
SAFE_RATIO = 0.80

SIZES = (192, 512)


def build(size: int) -> Image.Image:
    logo = Image.open(SRC).convert("RGBA")
    canvas = Image.new("RGBA", (size, size), BACKGROUND)

    inner = int(size * SAFE_RATIO)
    # Fit inside the safe box without distorting the crest; LANCZOS because this
    # is an upscale from 111px and nearest-neighbour would show every pixel.
    scale = min(inner / logo.width, inner / logo.height)
    new_size = (max(1, int(logo.width * scale)), max(1, int(logo.height * scale)))
    logo = logo.resize(new_size, Image.LANCZOS)

    offset = ((size - logo.width) // 2, (size - logo.height) // 2)
    canvas.paste(logo, offset, logo)
    return canvas


if __name__ == "__main__":
    for size in SIZES:
        path = os.path.join(OUT_DIR, f"icon-{size}.png")
        build(size).save(path, "PNG", optimize=True)
        print(f"  wrote {path}  ({size}x{size}, maskable)")
    print("\nDone. Referenced by /manifest.webmanifest (see sc_assistant/pwa.py).")

"""Generates the Airlog and Airlog Viewer icons (assets/*.png, packaging/*.ico, *.icns); outputs are committed."""
import math
from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]


def draw(size: int, viewer: bool = False) -> Image.Image:
    s = size * 4                                        # supersample for smooth edges
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = s * 0.22
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=r, fill=(22, 24, 29, 255))
    mid = s / 2
    bars = 9
    bw = s * 0.06
    gap = (s * 0.72 - bars * bw) / (bars - 1)
    x = s * 0.14
    for i in range(bars):
        h = (0.18 + 0.62 * abs(math.sin(i * 0.9 + 0.6)) ** 1.3) * s * 0.5
        if x + bw > s * 0.70:
            h = min(h, s * 0.20)                    # keep clear of the REC dot
        d.rounded_rectangle([x, mid - h, x + bw, mid + h], radius=bw / 2, fill=(245, 166, 35, 255))
        x += bw + gap
    if viewer:   # Airlog Viewer: a blue "play" triangle
        d.polygon([(s * 0.74, s * 0.09), (s * 0.74, s * 0.29), (s * 0.92, s * 0.19)], fill=(66, 153, 225, 255))
    else:
        d.ellipse([s * 0.73, s * 0.09, s * 0.91, s * 0.27], fill=(229, 72, 77, 255))   # REC dot
    return img.resize((size, size), Image.LANCZOS)


big = draw(256)
big.save(ROOT / "airlog" / "assets" / "icon.png")
big.save(ROOT / "packaging" / "airlog.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
draw(1024).save(ROOT / "packaging" / "airlog.icns")      # macOS app icon (Pillow writes every size from 1024)
view = draw(256, viewer=True)
view.save(ROOT / "airlog" / "assets" / "viewer.png")
view.save(ROOT / "packaging" / "viewer.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
draw(1024, viewer=True).save(ROOT / "packaging" / "viewer.icns")
print("icons written")

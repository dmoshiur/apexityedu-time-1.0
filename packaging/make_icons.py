#!/usr/bin/env python3
"""Build Windows ICO / BMP / favicon assets from the branding PNGs.

Run from the repository root:
    python packaging/make_icons.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    sys.stderr.write("Pillow is required:  pip install pillow\n")
    raise SystemExit(1)

ROOT = Path(__file__).resolve().parent.parent
BRAND = ROOT / "branding"
DASH_STATIC = ROOT / "apexity_bridge" / "dashboard" / "static"
ICO_SIZES = [(16, 16), (20, 20), (24, 24), (32, 32), (40, 40),
             (48, 48), (64, 64), (96, 96), (128, 128), (256, 256)]


def cover(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    tw, th = size
    scale = max(tw / img.width, th / img.height)
    nw, nh = max(1, int(img.width * scale)), max(1, int(img.height * scale))
    img = img.resize((nw, nh), Image.Resampling.LANCZOS)
    left, top = (nw - tw) // 2, (nh - th) // 2
    return img.crop((left, top, left + tw, top + th))


def save_ico(src: Path, dest: Path) -> None:
    img = Image.open(src).convert("RGBA")
    # Pillow writes a multi-resolution ICO from the sizes= list.
    img.save(dest, format="ICO", sizes=ICO_SIZES)
    print(f"  ico  {dest.relative_to(ROOT)}  ({dest.stat().st_size} bytes)")


def save_bmp(src: Path, dest: Path, size: tuple[int, int]) -> None:
    img = cover(Image.open(src).convert("RGB"), size)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, format="BMP")
    print(f"  bmp  {dest.relative_to(ROOT)}  {size[0]}x{size[1]}")


def main() -> int:
    icon_png = BRAND / "app-icon.png"
    mark_png = BRAND / "logo-mark.png"
    side_png = BRAND / "installer-sidebar.png"
    for p in (icon_png, mark_png, side_png):
        if not p.exists():
            sys.stderr.write(f"missing {p}\n")
            return 1

    BRAND.mkdir(parents=True, exist_ok=True)
    DASH_STATIC.mkdir(parents=True, exist_ok=True)

    save_ico(icon_png, BRAND / "app-icon.ico")
    save_ico(mark_png, BRAND / "favicon.ico")
    save_bmp(side_png, BRAND / "installer-sidebar.bmp", (164, 314))
    save_bmp(mark_png, BRAND / "installer-header.bmp", (64, 64))

    shutil.copy2(BRAND / "favicon.ico", DASH_STATIC / "favicon.ico")
    Image.open(mark_png).convert("RGBA").resize((128, 128), Image.Resampling.LANCZOS).save(
        DASH_STATIC / "logo.png", optimize=True)
    Image.open(icon_png).convert("RGBA").resize((256, 256), Image.Resampling.LANCZOS).save(
        DASH_STATIC / "app-icon.png", optimize=True)
    print(f"  png  {(DASH_STATIC / 'logo.png').relative_to(ROOT)}")
    print("icons ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

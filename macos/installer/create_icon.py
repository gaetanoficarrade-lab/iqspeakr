#!/usr/bin/env python3
"""Erstellt ein einfaches App-Icon für IQspeakr."""

import subprocess
import os
import tempfile

APP_DIR = os.path.dirname(os.path.abspath(__file__))

# SVG für ein Mikrofon-Icon (mit macOS-typischem Padding)
# Canvas 512x512, Icon-Fläche 410x410 zentriert (10% Padding)
SVG = """<?xml version="1.0" encoding="UTF-8"?>
<svg width="512" height="512" viewBox="0 0 512 512" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#6366f1"/>
      <stop offset="100%" stop-color="#4f46e5"/>
    </linearGradient>
  </defs>
  <!-- Hintergrund mit Padding (51px jede Seite) -->
  <rect x="51" y="51" width="410" height="410" rx="90" fill="url(#bg)"/>
  <!-- Mikrofon-Körper -->
  <rect x="220" y="140" width="72" height="150" rx="36" fill="white"/>
  <!-- Bügel -->
  <path d="M 184 260 Q 184 350 256 350 Q 328 350 328 260"
        fill="none" stroke="white" stroke-width="22" stroke-linecap="round"/>
  <!-- Ständer -->
  <line x1="256" y1="350" x2="256" y2="400" stroke="white" stroke-width="22" stroke-linecap="round"/>
  <line x1="212" y1="400" x2="300" y2="400" stroke="white" stroke-width="22" stroke-linecap="round"/>
</svg>"""


def create_icon():
    icon_dir = os.path.join(APP_DIR, "icon.iconset")
    os.makedirs(icon_dir, exist_ok=True)

    # SVG als temporäre Datei
    with tempfile.NamedTemporaryFile(suffix=".svg", delete=False, mode="w") as f:
        f.write(SVG)
        svg_path = f.name

    try:
        # Verschiedene Größen für iconset erstellen
        # Versuche rsvg-convert, sonst Pillow-Fallback
        rsvg_worked = False
        try:
            sizes = [16, 32, 64, 128, 256, 512]
            for size in sizes:
                png_path = os.path.join(icon_dir, f"icon_{size}x{size}.png")
                subprocess.run([
                    "rsvg-convert", "-w", str(size), "-h", str(size), svg_path, "-o", png_path
                ], capture_output=True, check=True)
                if size <= 256:
                    png_path_2x = os.path.join(icon_dir, f"icon_{size}x{size}@2x.png")
                    subprocess.run([
                        "rsvg-convert", "-w", str(size * 2), "-h", str(size * 2), svg_path, "-o", png_path_2x
                    ], capture_output=True, check=True)
            rsvg_worked = True
        except (FileNotFoundError, subprocess.CalledProcessError):
            pass

        if not rsvg_worked:
            print("rsvg-convert nicht verfügbar, nutze Pillow-Fallback...")
            _create_png_fallback(icon_dir)

        # iconset -> icns konvertieren
        icns_path = os.path.join(APP_DIR, "IQspeakr.icns")
        subprocess.run(["iconutil", "-c", "icns", icon_dir, "-o", icns_path], check=True)
        print(f"Icon erstellt: {icns_path}")

    finally:
        os.unlink(svg_path)
        # iconset aufräumen
        import shutil
        shutil.rmtree(icon_dir, ignore_errors=True)


def _draw_icon(draw, size):
    """Zeichnet das speakr-Icon mit macOS-typischem Padding."""
    # 10% Padding auf jeder Seite
    pad = int(size * 0.10)
    icon_size = size - 2 * pad

    # Hintergrund mit abgerundeten Ecken
    r = int(icon_size * 0.22)
    draw.rounded_rectangle(
        [pad, pad, pad + icon_size - 1, pad + icon_size - 1],
        radius=r, fill="#4f46e5"
    )

    # Alle Positionen relativ zum Icon-Bereich
    cx = size // 2
    mic_w = int(icon_size * 0.09)
    mic_h = int(icon_size * 0.18)
    mic_top = pad + int(icon_size * 0.22)

    # Mikrofon-Körper
    draw.rounded_rectangle(
        [cx - mic_w, mic_top, cx + mic_w, mic_top + mic_h * 2],
        radius=mic_w, fill="white",
    )

    # Bügel
    arc_y = mic_top + mic_h
    arc_r = int(icon_size * 0.18)
    draw.arc(
        [cx - arc_r, arc_y - arc_r // 2, cx + arc_r, arc_y + arc_r * 2],
        start=0, end=180, fill="white",
        width=max(2, icon_size // 20),
    )

    # Ständer
    stand_top = arc_y + arc_r * 2 - arc_r // 4
    stand_bottom = stand_top + int(icon_size * 0.10)
    lw = max(2, icon_size // 20)
    draw.line([cx, stand_top, cx, stand_bottom], fill="white", width=lw)
    foot_w = int(icon_size * 0.11)
    draw.line([cx - foot_w, stand_bottom, cx + foot_w, stand_bottom], fill="white", width=lw)


def _create_png_fallback(icon_dir):
    """Erstellt PNGs ohne externe SVG-Tools."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("Pillow nicht installiert. Installiere mit: pip install Pillow")
        print("Oder: brew install librsvg")
        return

    sizes = [16, 32, 64, 128, 256, 512]
    for size in sizes:
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        _draw_icon(draw, size)
        img.save(os.path.join(icon_dir, f"icon_{size}x{size}.png"))

        if size <= 256:
            s = size * 2
            img2 = Image.new("RGBA", (s, s), (0, 0, 0, 0))
            draw2 = ImageDraw.Draw(img2)
            _draw_icon(draw2, s)
            img2.save(os.path.join(icon_dir, f"icon_{size}x{size}@2x.png"))


if __name__ == "__main__":
    create_icon()

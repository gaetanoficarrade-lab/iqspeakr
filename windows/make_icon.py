r"""
Generiert icon.ico (schwarzer Hintergrund, weisses Mikrofon) fuer IQspeakr.

Aufruf:
    .\.venv\Scripts\python.exe make_icon.py

Schreibt:
    icon.ico          - Multi-Resolution-Icon (16..256), fuer PyInstaller + Inno Setup
    icon_preview.png  - 256x256 Preview zum Anschauen
"""
from PIL import Image, ImageDraw

SIZE = 256
BG = (0, 0, 0, 255)
FG = (255, 255, 255, 255)


def draw_mic(size: int = SIZE) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Hintergrund: schwarzes, leicht abgerundetes Quadrat (Windows-11-Look).
    corner = int(size * 0.18)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=corner, fill=BG)

    # Strichstaerke fuer Stand/Stem/Base
    stroke = max(3, int(size * 0.055))

    # --- Mikrofon-Kapsel (Kopf) ---
    cap_w = int(size * 0.34)
    cap_h = int(size * 0.48)
    cap_x0 = (size - cap_w) // 2
    cap_y0 = int(size * 0.14)
    cap_x1 = cap_x0 + cap_w
    cap_y1 = cap_y0 + cap_h
    d.rounded_rectangle([cap_x0, cap_y0, cap_x1, cap_y1],
                        radius=cap_w // 2, fill=FG)

    # --- U-Bogen (Stand, umarmt die untere Haelfte der Kapsel) ---
    arc_pad = int(size * 0.10)
    arc_x0 = cap_x0 - arc_pad
    arc_x1 = cap_x1 + arc_pad
    arc_y0 = cap_y0 + int(cap_h * 0.45)
    arc_y1 = cap_y1 + arc_pad
    d.arc([arc_x0, arc_y0, arc_x1, arc_y1],
          start=0, end=180, fill=FG, width=stroke)

    # --- Stem: vom unteren Ende des U-Bogens zum Fuss ---
    stem_x = size // 2
    stem_y0 = arc_y1 - stroke // 2
    stem_y1 = int(size * 0.86)
    d.line([(stem_x, stem_y0), (stem_x, stem_y1)], fill=FG, width=stroke)

    # --- Fuss: horizontaler Strich ---
    base_half = int(size * 0.14)
    d.line([(stem_x - base_half, stem_y1),
            (stem_x + base_half, stem_y1)], fill=FG, width=stroke)

    return img


def main() -> None:
    # Grossversion fuer ICO-Generierung + Preview
    master = draw_mic(256)
    master.save("icon_preview.png", format="PNG")

    # Einzelne Groessen fuer saubere ICO-Pyramide (PIL skaliert sonst nur runter)
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    images = [draw_mic(s) for s in sizes]
    images[-1].save(
        "icon.ico",
        format="ICO",
        sizes=[(s, s) for s in sizes],
        append_images=images[:-1],
    )
    print(f"icon.ico geschrieben ({len(sizes)} Aufloesungen)")
    print("icon_preview.png geschrieben (256x256)")


if __name__ == "__main__":
    main()

r"""
Generiert icon.ico (lila Gradient, weisses Mikrofon im Apple-Style) fuer IQspeakr.

Aufruf:
    .\.venv\Scripts\python.exe make_icon.py

Schreibt:
    icon.ico          - Multi-Resolution-Icon (16..256), fuer PyInstaller + Inno Setup
    icon_preview.png  - 256x256 Preview zum Anschauen
"""
from PIL import Image, ImageDraw

SIZE = 256

# Apple-Style Lila-Verlauf: oben heller (purple-500), unten dunkler (purple-700).
# Subtiler Vertical-Gradient gibt dem Icon die typische Apple-Tiefe.
BG_TOP = (168, 85, 247, 255)    # #A855F7  (Tailwind purple-500)
BG_BOT = (109, 40, 217, 255)    # #6D28D9  (Tailwind purple-700)
FG = (255, 255, 255, 255)


def _gradient_rounded_square(size, radius, top, bot):
    """Vertical-Gradient gefuelltes, abgerundetes Quadrat. Erzeugt eine ueber
    den ganzen Hintergrund verlaufende Lila-Flaeche und maskiert sie auf den
    Rounded-Square."""
    # 1) Vertical-Gradient als RGBA-Bild.
    grad = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    px = grad.load()
    for y in range(size):
        t = y / max(1, size - 1)
        r = int(top[0] * (1 - t) + bot[0] * t)
        g = int(top[1] * (1 - t) + bot[1] * t)
        b = int(top[2] * (1 - t) + bot[2] * t)
        for x in range(size):
            px[x, y] = (r, g, b, 255)

    # 2) Rounded-Square-Alpha-Maske.
    mask = Image.new("L", (size, size), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)

    # 3) Gradient durch die Maske maskieren -> abgerundeter Lila-Verlauf.
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(grad, (0, 0), mask=mask)
    return out


def draw_mic(size: int = SIZE) -> Image.Image:
    # Hintergrund: lila Gradient mit Apple-typischem 22%-Eckradius.
    corner = int(size * 0.22)
    img = _gradient_rounded_square(size, corner, BG_TOP, BG_BOT)
    d = ImageDraw.Draw(img)

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

    # Einzelne Groessen fuer saubere ICO-Pyramide (PIL skaliert sonst nur runter).
    # Bei sehr kleinen Groessen (16/20/24) macht der Gradient kaum noch einen
    # Unterschied — sieht aber trotzdem sauber aus, weil PIL pro Groesse neu
    # zeichnet.
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

"""Generate the app's PWA icons.

Run once (or whenever the design changes) to (re)produce static/*.png:
    ./venv/bin/python generate_icons.py

Draws the same arrow-cursor glyph used for the on-screen virtual cursor
(see HTML_PAGE's #cursorDot SVG in server.py) over a simple monitor shape,
so the home-screen icon actually looks like this app. Background fills
the canvas edge-to-edge with the glyph kept inside a centered safe zone,
so the same 512px image works as both a normal and a "maskable" Android
icon (the OS may crop anything outside that zone to a circle/squircle/etc).
"""
from PIL import Image, ImageDraw

BG = (42, 99, 201, 255)      # matches the app's .active accent blue
FG = (255, 255, 255, 255)    # monitor + cursor
OUTLINE = (20, 40, 90, 255)  # subtle dark edge so the cursor reads over the monitor

CANVAS = 512


def draw_icon(bg_color=BG):
    # bg_color is parameterized so server.py can generate a differently
    # colored icon per machine at request time (see machine_icon_color()) —
    # this module has no Flask dependency, so server.py imports draw_icon()
    # from here rather than duplicating the drawing code.
    img = Image.new("RGBA", (CANVAS, CANVAS), bg_color)
    d = ImageDraw.Draw(img)

    # Monitor: rounded-rect outline, kept within the ~70% safe zone.
    mon = (96, 120, 384, 336)
    d.rounded_rectangle(mon, radius=22, outline=FG, width=24)

    # Cursor arrow (same silhouette as HTML_PAGE's #cursorDot SVG path,
    # scaled ~9x), tip pointing up-left, overlapping the monitor's
    # bottom-right corner.
    pts = [(0, 0), (0, 180), (45, 139.5), (76.5, 207), (103.5, 193.5),
           (72, 126), (126, 126)]
    ox, oy = 300, 250
    poly = [(x + ox, y + oy) for x, y in pts]
    d.polygon(poly, fill=FG, outline=OUTLINE, width=6)

    return img


def main():
    icon = draw_icon()
    icon.save("static/icon-512.png")
    icon.resize((192, 192), Image.LANCZOS).save("static/icon-192.png")
    icon.resize((180, 180), Image.LANCZOS).save("static/apple-touch-icon.png")
    print("Wrote static/icon-512.png, static/icon-192.png, static/apple-touch-icon.png")


if __name__ == "__main__":
    main()

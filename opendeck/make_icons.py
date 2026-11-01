#!/usr/bin/env python3
"""Generate the plugin's icon PNGs (needs pycairo). Run from this directory."""
import math
import os
import sys

import cairo

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.join(HERE, "io.github.lawsonf.audiomixer.sdPlugin")
sys.path.insert(0, PLUGIN_DIR)
import plugin  # noqa: E402

BLUE = plugin.BLUE


def surface(size, painter):
    s = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(s)
    cr.scale(size / 144, size / 144)
    painter(cr)
    return s


def speaker(cr):
    plugin.draw_glyph(cr, BLUE, waves=3)


def mix(cr):          # speaker + three slider lines
    cr.save(); cr.translate(-10, -6); cr.scale(.9, .9); plugin.draw_glyph(cr, BLUE, waves=0); cr.restore()
    cr.set_source_rgb(*BLUE); cr.set_line_width(8); cr.set_line_cap(1)
    for y, x in ((46, 104), (72, 118), (98, 96)):
        cr.move_to(88, y); cr.line_to(132, y); cr.stroke()
        cr.arc(x - 6, y, 9, 0, 2 * math.pi); cr.fill()


def system(cr):       # monitor
    cr.set_source_rgb(*BLUE); cr.set_line_width(9); cr.set_line_join(1); cr.set_line_cap(1)
    cr.rectangle(20, 30, 104, 70); cr.stroke()
    cr.move_to(72, 100); cr.line_to(72, 120); cr.stroke()
    cr.move_to(48, 122); cr.line_to(96, 122); cr.stroke()


def window(cr):       # window with a title bar
    cr.set_source_rgb(*BLUE); cr.set_line_width(9); cr.set_line_join(1); cr.set_line_cap(1)
    cr.rectangle(20, 28, 104, 88); cr.stroke()
    cr.move_to(20, 56); cr.line_to(124, 56); cr.stroke()
    cr.arc(38, 42, 4, 0, 2 * math.pi); cr.fill()


def state(cr):
    cr.set_source_rgb(.13, .13, .13); cr.paint()
    plugin.draw_glyph(cr, BLUE, waves=3)


icons = os.path.join(PLUGIN_DIR, "icons")
os.makedirs(icons, exist_ok=True)
for name, fn, size in (("plugin", speaker, 256), ("channel", speaker, 72), ("mix", mix, 72),
                       ("system", system, 72), ("window", window, 72), ("state", state, 72)):
    surface(size, fn).write_to_png(os.path.join(icons, f"{name}.png"))
    if size == 72:
        surface(144, fn).write_to_png(os.path.join(icons, f"{name}@2x.png"))
print("icons written to", icons)

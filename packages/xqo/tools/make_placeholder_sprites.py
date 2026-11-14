#!/usr/bin/env python3
"""WARNING: this overwrites the real art in office/citizens/ (*_idle.png, *_stand.png). Only run it for a new character.

Placeholder 2D sprites for the idle routines. Run from the repo root: python tools/make_placeholder_sprites.py

For each character it writes two 256x256 sheets (4x4 frames of 64 px) next to the existing ones, built from the
character's own frames plus a few flat-colour pixels for the props, so they are easy to repaint by hand:

  <name>_idle.png   row 0 read (4)  row 1 drink (4)  row 2 tend / water a plant (4)  row 3 write (4)
  <name>_stand.png  row 0 idle_left (2)  row 1 idle_right (2)  row 2 sit_down (2)  row 3 sit_up (2)

Replace the PNGs with real art, keeping the grid; the names and rows are registered in office2d.js (charSprites).
"""
from pathlib import Path

from PIL import Image, ImageDraw

DIR = Path(__file__).resolve().parent.parent / "xqo/plugins/office/static/office/citizens"
F = 64
BOOK, PAGE, CUP, STEAM, CAN, WATER, PEN, PAD = "#7a4a2b", "#f4efe2", "#f2f2f2", "#cfd8e3", "#6f8aa3", "#5fb4ff", "#222222", "#ffffff"


def frame(sheet, col, row):
    return sheet.crop((col * F, row * F, (col + 1) * F, (row + 1) * F))


def shifted(img, dx=0, dy=0):
    out = Image.new("RGBA", (F, F), (0, 0, 0, 0))
    out.paste(img, (dx, dy), img)
    return out


def sat(img, rows_from, keep=0.5, drop=6):
    """Sitting from the front: the upper body lowered, the legs squashed (placeholder for a real sitting pose)."""
    legs = img.crop((0, rows_from, F, F))
    legs = legs.resize((F, max(1, int(legs.height * keep))), Image.NEAREST)
    out = Image.new("RGBA", (F, F), (0, 0, 0, 0))
    out.paste(img.crop((0, 0, F, rows_from)), (0, drop), img.crop((0, 0, F, rows_from)))
    out.paste(legs, (0, rows_from + drop), legs)
    return out


def read(base, i):
    f = shifted(base[i % 4], 0, 1 if i % 2 else 0)
    d = ImageDraw.Draw(f)
    d.rectangle((25, 38, 38, 46), fill=BOOK)                      # the book, held in front of the chest
    d.rectangle((26, 39, 37, 45), fill=PAGE)
    d.line((31, 39, 31, 45), fill=BOOK)
    if i in (1, 3):                                               # a page turns
        d.polygon([(31, 39), (36 if i == 1 else 28, 37), (36 if i == 1 else 28, 43), (31, 45)], fill="#e1dccb")
    return f


def drink(base, i):
    f = shifted(base[i % 4])
    d = ImageDraw.Draw(f)
    y = (40, 34, 27, 34)[i]                                       # the cup goes up to the mouth and down again
    d.rectangle((38, y, 42, y + 5), fill=CUP)
    d.rectangle((42, y + 1, 43, y + 3), outline=CUP)
    d.point((39, y - 2) if i % 2 else (40, y - 3), fill=STEAM)
    return f


def tend(base, i):
    f = shifted(base[i % 4], 0, 1 if i % 2 else 0)
    d = ImageDraw.Draw(f)
    d.rectangle((36, 36, 44, 41), fill=CAN)                       # a watering can, tipped towards the plant
    d.line((44, 37, 49, 34), fill=CAN, width=2)
    for k in range(3):                                            # drops falling
        d.point((50 + (k + i) % 2, 36 + ((i + k * 2) % 4) * 3), fill=WATER)
    return f


def write(back, i):
    f = shifted(back[i % 4])
    d = ImageDraw.Draw(f)
    x = 40 + (0, 2, 1, 3)[i]                                      # the pen scribbles on the board, above the shoulder
    d.rectangle((x, 20, x + 1, 27), fill=PEN)
    d.line((x + 2, 18, x + 8, 18 + (i % 2) * 2), fill="#d03b3b")
    return f


def sheet(rows):
    img = Image.new("RGBA", (4 * F, 4 * F), (0, 0, 0, 0))
    for r, frames in enumerate(rows):
        for c, fr in enumerate(frames):
            img.paste(fr, (c * F, r * F), fr)
    return img


def make(name):
    walk = Image.open(DIR / f"{name}_walk.png").convert("RGBA")
    acts = Image.open(DIR / f"{name}_actions.png").convert("RGBA")
    front = [frame(acts, c, 3) for c in range(4)]                 # standing, facing us
    back = [frame(walk, c, 1) for c in range(4)]
    left, right = [frame(walk, c, 2) for c in range(4)], [frame(walk, c, 3) for c in range(4)]
    sheet([[read(front, i) for i in range(4)], [drink(front, i) for i in range(4)],
           [tend(front, i) for i in range(4)], [write(back, i) for i in range(4)]]).save(DIR / f"{name}_idle.png")
    sheet([[shifted(left[1]), shifted(left[3])], [shifted(right[1]), shifted(right[3])],
           [sat(front[0], 40), sat(front[2], 40, drop=7)], [sat(back[0], 40), sat(back[2], 40, drop=7)]]).save(DIR / f"{name}_stand.png")


if __name__ == "__main__":
    for n in ("nova", "rio", "dexter", "morty"):
        make(n)
        print("wrote", n)

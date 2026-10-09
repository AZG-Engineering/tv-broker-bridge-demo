#!/usr/bin/env python3
"""Render an asciinema v2 ``.cast`` recording to an animated GIF.

A small stand-in for ``agg`` (which could not be installed where these demos were made):
pyte emulates the terminal, Pillow draws it. Dark theme, window-style title bar, one fixed
colour palette (so unchanged pixels stay identical from frame to frame and the file stays small).

Needs ``pip install pyte pillow`` and DejaVu Sans Mono (``fonts-dejavu-core`` on Debian/Ubuntu).
Usage: ``python cast_to_gif.py demo.cast demo.gif [--font-size 20] [--title TEXT]``
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pyte
from PIL import Image, ImageDraw, ImageFont

FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")
BG = "#0d1117"
FG = "#e6edf3"
TITLEBAR = "#161b22"
TITLE_FG = "#8b949e"
CURSOR = "#e6edf3"
ANSI = {
    "black": "#484f58",
    "red": "#ff7b72",
    "green": "#3fb950",
    "brown": "#e3b341",
    "blue": "#58a6ff",
    "magenta": "#bc8cff",
    "cyan": "#39c5cf",
    "white": "#b1bac4",
    "brightblack": "#6e7681",
    "brightred": "#ffa198",
    "brightgreen": "#56d364",
    "brightbrown": "#f2cc60",
    "brightblue": "#79c0ff",
    "brightmagenta": "#d2a8ff",
    "brightcyan": "#56d4dd",
    "brightwhite": "#f0f6fc",
}
RAMP_LEVELS = 16  # shades per (foreground, background) pair, for anti-aliased glyph edges
PAD = 24
TITLE_H = 44
FRAME_STEP_S = 0.04  # sample the screen at 25 fps; unchanged samples merge into one GIF frame
MIN_DELAY_MS = 40


def to_rgb(color: str) -> tuple[int, int, int]:
    value = color.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def resolve(color: str, default: str) -> str:
    if color == "default":
        return default
    if color in ANSI:
        return ANSI[color]
    return "#" + color  # pyte reports 256-colour and truecolour values as "rrggbb"


@dataclass
class Fonts:
    regular: ImageFont.FreeTypeFont
    bold: ImageFont.FreeTypeFont
    ui: ImageFont.FreeTypeFont
    cell_w: float
    cell_h: int
    y_off: int

    @classmethod
    def load(cls, size: int) -> Fonts:
        regular = ImageFont.truetype(str(FONT_DIR / "DejaVuSansMono.ttf"), size)
        bold = ImageFont.truetype(str(FONT_DIR / "DejaVuSansMono-Bold.ttf"), size)
        ui = ImageFont.truetype(str(FONT_DIR / "DejaVuSans.ttf"), max(14, size - 4))
        ascent, descent = regular.getmetrics()
        cell_h = round(size * 1.32)
        return cls(
            regular, bold, ui, regular.getlength("M"), cell_h, (cell_h - ascent - descent) // 2
        )


@dataclass
class Frame:
    rows: tuple[tuple[pyte.screens.Char, ...], ...]
    cursor: tuple[int, int] | None
    duration_s: float = 0.0


@dataclass
class Recording:
    cols: int
    rows: int
    events: list[tuple[float, str]] = field(default_factory=list)


def read_cast(path: Path) -> Recording:
    lines = path.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    if header.get("version") != 2:
        msg = f"unsupported cast version: {header.get('version')}"
        raise ValueError(msg)
    rec = Recording(cols=int(header["width"]), rows=int(header["height"]))
    for line in lines[1:]:
        if not line.strip():
            continue
        t, kind, data = json.loads(line)
        if kind == "o":
            rec.events.append((float(t), str(data)))
    return rec


def capture_frames(rec: Recording, tail_hold_s: float) -> list[Frame]:
    """Replay the recording and return one Frame per distinct screen state."""
    screen = pyte.Screen(rec.cols, rec.rows)
    stream = pyte.Stream(screen)
    end = rec.events[-1][0] + tail_hold_s
    frames: list[Frame] = []
    index = 0
    for tick in range(math.ceil(end / FRAME_STEP_S) + 1):
        now = tick * FRAME_STEP_S
        fed = False
        while index < len(rec.events) and rec.events[index][0] <= now:
            stream.feed(rec.events[index][1])
            index += 1
            fed = True
        if fed or not frames:
            rows = tuple(
                tuple(screen.buffer[y][x] for x in range(rec.cols)) for y in range(rec.rows)
            )
            cursor = None if screen.cursor.hidden else (screen.cursor.x, screen.cursor.y)
            if not frames or frames[-1].rows != rows or frames[-1].cursor != cursor:
                frames.append(Frame(rows, cursor))
        frames[-1].duration_s += FRAME_STEP_S
    while len(frames) > 1 and is_blank(frames[0]):
        frames.pop(0)  # the cast starts with a `clear`; the first frame should show content
    return frames


def is_blank(frame: Frame) -> bool:
    return all(not char.data.strip() for row in frame.rows for char in row)


def cell_colors(char: pyte.screens.Char) -> tuple[str, str]:
    fg = resolve(char.fg, FG)
    bg = resolve(char.bg, BG)
    if char.reverse:
        fg, bg = bg, fg
    return fg, bg


def ramp(fg: str, bg: str) -> list[tuple[int, int, int]]:
    (r0, g0, b0), (r1, g1, b1) = to_rgb(bg), to_rgb(fg)
    steps = [i / (RAMP_LEVELS - 1) for i in range(RAMP_LEVELS)]
    return [
        (round(r0 + (r1 - r0) * t), round(g0 + (g1 - g0) * t), round(b0 + (b1 - b0) * t))
        for t in steps
    ]


def build_palette(frames: list[Frame]) -> Image.Image:
    """One palette for the whole animation: a shade ramp for every colour pair in use."""
    pairs = {(FG, BG), (TITLE_FG, TITLEBAR), (BG, CURSOR), (FG, CURSOR)}
    for frame in frames:
        for row in frame.rows:
            for char in row:
                pairs.add(cell_colors(char))
    colors: list[tuple[int, int, int]] = []
    for fg, bg in sorted(pairs):
        for rgb in ramp(fg, bg):
            if rgb not in colors:
                colors.append(rgb)
    for extra in ("#ff5f56", "#ffbd2e", "#27c93f", BG, TITLEBAR):
        if to_rgb(extra) not in colors:
            colors.append(to_rgb(extra))
    if len(colors) > 256:
        msg = f"{len(colors)} palette colours needed; lower RAMP_LEVELS or use fewer colours"
        raise ValueError(msg)
    flat = [c for rgb in colors for c in rgb]
    palette = Image.new("P", (1, 1))
    palette.putpalette(flat + [0] * (768 - len(flat)))
    return palette


class Renderer:
    def __init__(self, rec: Recording, fonts: Fonts, palette: Image.Image, title: str) -> None:
        self.rec = rec
        self.fonts = fonts
        self.palette = palette
        self.width = round(rec.cols * fonts.cell_w) + 2 * PAD
        self.width += self.width % 2
        self.height = TITLE_H + 2 * PAD + rec.rows * fonts.cell_h
        self.height += self.height % 2
        self.strip_cache: dict[tuple[object, int | None], Image.Image] = {}
        self.chrome = self._chrome(title)

    def _to_p(self, image: Image.Image) -> Image.Image:
        return image.quantize(palette=self.palette, dither=Image.Dither.NONE)

    def _chrome(self, title: str) -> Image.Image:
        image = Image.new("RGB", (self.width, self.height), BG)
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, self.width, TITLE_H), fill=TITLEBAR)
        for i, color in enumerate(("#ff5f56", "#ffbd2e", "#27c93f")):
            cx, cy = PAD + 8 + i * 26, TITLE_H // 2
            draw.ellipse((cx - 7, cy - 7, cx + 7, cy + 7), fill=color)
        text_w = draw.textlength(title, font=self.fonts.ui)
        draw.text(
            ((self.width - text_w) / 2, TITLE_H // 2),
            title,
            font=self.fonts.ui,
            fill=TITLE_FG,
            anchor="lm",
        )
        return self._to_p(image)

    def row_strip(self, row: tuple[pyte.screens.Char, ...], cursor_x: int | None) -> Image.Image:
        key = (row, cursor_x)
        cached = self.strip_cache.get(key)
        if cached is not None:
            return cached
        f = self.fonts
        strip = Image.new("RGB", (round(self.rec.cols * f.cell_w), f.cell_h), BG)
        draw = ImageDraw.Draw(strip)
        for x, char in enumerate(row):
            fg, bg = cell_colors(char)
            if x == cursor_x:
                fg, bg = BG, CURSOR
            left = round(x * f.cell_w)
            if bg != BG:
                draw.rectangle((left, 0, round((x + 1) * f.cell_w) - 1, f.cell_h), fill=bg)
            if char.data.strip():
                font = f.bold if char.bold else f.regular
                draw.text((left, f.y_off), char.data, font=font, fill=fg)
        result = self._to_p(strip)
        self.strip_cache[key] = result
        return result

    def render(self, frame: Frame) -> Image.Image:
        image = self.chrome.copy()
        for y, row in enumerate(frame.rows):
            cursor_x = frame.cursor[0] if frame.cursor and frame.cursor[1] == y else None
            image.paste(self.row_strip(row, cursor_x), (PAD, TITLE_H + PAD + y * self.fonts.cell_h))
        return image


def write_gif(images: list[Image.Image], durations_ms: list[int], out: Path) -> None:
    images[0].save(
        out,
        save_all=True,
        append_images=images[1:],
        duration=durations_ms,
        loop=0,
        optimize=False,
        disposal=1,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cast", type=Path)
    parser.add_argument("gif", type=Path)
    parser.add_argument("--font-size", type=int, default=20)
    parser.add_argument("--title", default="tv-broker-bridge demo")
    parser.add_argument("--tail-hold", type=float, default=0.0, help="extra seconds on last frame")
    args = parser.parse_args(argv)

    rec = read_cast(args.cast)
    frames = capture_frames(rec, args.tail_hold)
    palette = build_palette(frames)
    renderer = Renderer(rec, Fonts.load(args.font_size), palette, args.title)
    images = [renderer.render(f) for f in frames]
    durations = [max(MIN_DELAY_MS, round(f.duration_s * 1000 / 10) * 10) for f in frames]
    write_gif(images, durations, args.gif)

    total_s = sum(durations) / 1000
    size_mb = args.gif.stat().st_size / 1e6
    print(
        f"{args.gif}: {renderer.width}x{renderer.height}, {len(frames)} frames, "
        f"{total_s:.1f} s, {size_mb:.2f} MB"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

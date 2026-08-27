"""
Renders one size-chart image per category, from size_charts.py's data
(sourced from marksandspencer.com.tr/size-chart, 2026-08-27).

M&S's own size-chart page has no downloadable chart images -- only HTML
tables -- so there's nothing to source/download for this task (GitHub issue
#5, business instruction 2026-08-26: "create a size chart image for each
category getting it from marks&spencer"). This generates a clean table image
from that same M&S data instead, which is the closest fulfillment of the
instruction's intent given the source material that actually exists.

Output: one PNG per category key in size_charts.SIZE_CHARTS, saved to
OUTPUT_DIR. NOT yet wired into any live product listing -- pushing an image
onto a PDP needs a public image host, same blocker as generate_extra_photos.py
and the video task (GitHub issues #4/#8); this script only produces local
files today.
"""
import os
import sys

from PIL import Image, ImageDraw, ImageFont

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from size_charts import SIZE_CHARTS

OUTPUT_DIR = "generated_size_charts"
FONT_PATH = "C:/Windows/Fonts/arial.ttf"
FONT_BOLD_PATH = "C:/Windows/Fonts/arialbd.ttf"

TITLE_SIZE = 28
HEADER_SIZE = 20
CELL_SIZE = 20
PADDING = 24
ROW_HEIGHT = 44
HEADER_ROW_HEIGHT = 48
TITLE_HEIGHT = 64

BG_COLOR = (255, 255, 255)
HEADER_BG = (30, 41, 59)       # dark slate
HEADER_FG = (255, 255, 255)
ROW_BG_EVEN = (255, 255, 255)
ROW_BG_ODD = (241, 245, 249)   # light slate
TEXT_COLOR = (15, 23, 42)
BORDER_COLOR = (203, 213, 225)
BRAND_COLOR = (15, 23, 42)


def measure_column_widths(columns, rows, header_font, cell_font, draw):
    widths = []
    for i, col in enumerate(columns):
        header_w = draw.textlength(col, font=header_font)
        cell_w = max((draw.textlength(row[i], font=cell_font) for row in rows), default=0)
        widths.append(int(max(header_w, cell_w) + PADDING * 2))
    return widths


def render_chart(category_key, chart, output_path):
    title = chart["title"]
    columns = chart["columns"]
    rows = chart["rows"]

    title_font = ImageFont.truetype(FONT_BOLD_PATH, TITLE_SIZE)
    header_font = ImageFont.truetype(FONT_BOLD_PATH, HEADER_SIZE)
    cell_font = ImageFont.truetype(FONT_PATH, CELL_SIZE)

    # Measure on a throwaway canvas first, since column widths depend on
    # text extents which need a real ImageDraw instance.
    scratch = Image.new("RGB", (10, 10))
    scratch_draw = ImageDraw.Draw(scratch)
    col_widths = measure_column_widths(columns, rows, header_font, cell_font, scratch_draw)

    table_width = sum(col_widths)
    img_width = table_width + PADDING * 2
    img_height = TITLE_HEIGHT + HEADER_ROW_HEIGHT + ROW_HEIGHT * len(rows) + PADDING * 2

    img = Image.new("RGB", (img_width, img_height), BG_COLOR)
    draw = ImageDraw.Draw(img)

    # Title
    draw.text((PADDING, PADDING), title, font=title_font, fill=BRAND_COLOR)

    table_top = TITLE_HEIGHT + PADDING
    x = PADDING

    # Header row
    draw.rectangle([PADDING, table_top, PADDING + table_width, table_top + HEADER_ROW_HEIGHT],
                   fill=HEADER_BG)
    cx = PADDING
    for col, w in zip(columns, col_widths):
        text_w = draw.textlength(col, font=header_font)
        draw.text((cx + (w - text_w) / 2, table_top + (HEADER_ROW_HEIGHT - HEADER_SIZE) / 2 - 2),
                   col, font=header_font, fill=HEADER_FG)
        cx += w

    # Data rows
    y = table_top + HEADER_ROW_HEIGHT
    for i, row in enumerate(rows):
        bg = ROW_BG_EVEN if i % 2 == 0 else ROW_BG_ODD
        draw.rectangle([PADDING, y, PADDING + table_width, y + ROW_HEIGHT], fill=bg)
        cx = PADDING
        for val, w in zip(row, col_widths):
            text_w = draw.textlength(val, font=cell_font)
            draw.text((cx + (w - text_w) / 2, y + (ROW_HEIGHT - CELL_SIZE) / 2 - 2),
                       val, font=cell_font, fill=TEXT_COLOR)
            cx += w
        y += ROW_HEIGHT

    # Outer border + column separators
    draw.rectangle([PADDING, table_top, PADDING + table_width, table_top + HEADER_ROW_HEIGHT + ROW_HEIGHT * len(rows)],
                   outline=BORDER_COLOR, width=2)
    cx = PADDING
    for w in col_widths[:-1]:
        cx += w
        draw.line([(cx, table_top), (cx, table_top + HEADER_ROW_HEIGHT + ROW_HEIGHT * len(rows))],
                  fill=BORDER_COLOR, width=1)

    img.save(output_path, "PNG")


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    for category_key, chart in SIZE_CHARTS.items():
        out_path = os.path.join(OUTPUT_DIR, f"{category_key}.png")
        render_chart(category_key, chart, out_path)
        print(f"{category_key} -> {out_path}")
    print(f"\n{len(SIZE_CHARTS)} size chart image(s) generated in {OUTPUT_DIR}/.")
    print("Not yet uploaded anywhere -- needs a public image host before it can go on a live PDP "
          "(same blocker as generate_extra_photos.py, see GitHub issues #4/#8).")


if __name__ == "__main__":
    main()

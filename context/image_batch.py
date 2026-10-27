"""In-memory numbered image grids for one caption-model request."""

from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from ..channels.media import MAX_CAPTION_BYTES, ChannelError

MAX_BATCH_IMAGES = 6
TILE_EDGE = 2048
GAP = 32
LABEL_HEIGHT = 64
BACKGROUND = (30, 41, 59)
LABEL_BACKGROUND = (232, 238, 247)


def _label(canvas, index, x, y):
    text = f"IMAGE {index}"
    try:
        font = ImageFont.load_default(size=32)
    except TypeError:
        font = ImageFont.load_default()
    left, top, right, bottom = font.getbbox(text)
    with Image.new("L", (right - left, bottom - top), 0) as mask:
        ImageDraw.Draw(mask).text((-left, -top), text, font=font, fill=255)
        width = round(mask.width * 32 / mask.height)
        with mask.resize((width, 32), Image.Resampling.LANCZOS) as enlarged:
            canvas.paste((15, 23, 42), (x + 16, y + 16), enlarged)


def compose_images(sources: list[bytes]) -> tuple[bytes, dict]:
    if not 2 <= len(sources) <= MAX_BATCH_IMAGES:
        raise ChannelError("拼图需要 2–6 张图片")
    tiles = []
    try:
        for data in sources:
            with Image.open(io.BytesIO(data)) as original:
                if original.width * original.height > 40_000_000:
                    raise ChannelError("image dimensions are too large")
                with ImageOps.exif_transpose(original) as oriented:
                    oriented.thumbnail((TILE_EDGE, TILE_EDGE), Image.Resampling.LANCZOS)
                    with oriented.convert("RGBA") as rgba:
                        tile = Image.new("RGB", rgba.size, "white")
                        tile.paste(rgba, mask=rgba.getchannel("A"))
                        tiles.append(tile)
        columns = 2
        rows = (len(tiles) + columns - 1) // columns
        tile_width = max(256, *(tile.width for tile in tiles))
        tile_height = max(128, *(tile.height for tile in tiles))
        width = columns * tile_width + (columns + 1) * GAP
        height = rows * (tile_height + LABEL_HEIGHT) + (rows + 1) * GAP
        positions = []
        with Image.new("RGB", (width, height), BACKGROUND) as canvas:
            draw = ImageDraw.Draw(canvas)
            for index, tile in enumerate(tiles, 1):
                row, column = divmod(index - 1, columns)
                x = GAP + column * (tile_width + GAP)
                y = GAP + row * (tile_height + LABEL_HEIGHT + GAP)
                draw.rectangle(
                    (x, y, x + tile_width - 1, y + LABEL_HEIGHT - 1),
                    fill=LABEL_BACKGROUND,
                )
                draw.rectangle(
                    (
                        x,
                        y + LABEL_HEIGHT,
                        x + tile_width - 1,
                        y + LABEL_HEIGHT + tile_height - 1,
                    ),
                    fill="white",
                )
                _label(canvas, index, x, y)
                image_x = x + (tile_width - tile.width) // 2
                image_y = y + LABEL_HEIGHT + (tile_height - tile.height) // 2
                canvas.paste(tile, (image_x, image_y))
                positions.append(
                    {
                        "index": index,
                        "x": image_x,
                        "y": image_y,
                        "width": tile.width,
                        "height": tile.height,
                    }
                )
            output = io.BytesIO()
            canvas.save(output, format="JPEG", quality=92, subsampling=0)
        encoded = output.getvalue()
        if len(encoded) >= MAX_CAPTION_BYTES:
            raise ChannelError("拼图达到或超过 16 MB，请减少本组图片数量后重试")
        return encoded, {
            "width": width,
            "height": height,
            "columns": columns,
            "rows": rows,
            "gap_pixels": GAP,
            "label_height": LABEL_HEIGHT,
            "panels": positions,
        }
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
    ) as exc:
        raise ChannelError("图片无法解码或拼接") from exc
    finally:
        for tile in tiles:
            tile.close()

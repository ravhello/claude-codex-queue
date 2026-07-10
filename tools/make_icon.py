from __future__ import annotations

import struct
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "claude-queue.ico"
PNG32_OUT = ROOT / "assets" / "claude-queue-32.png"


def rgba(size: int) -> bytearray:
    data = bytearray(size * size * 4)

    def set_px(x: int, y: int, color: tuple[int, int, int, int]) -> None:
        if 0 <= x < size and 0 <= y < size:
            i = (y * size + x) * 4
            data[i : i + 4] = bytes(color)

    def rounded_rect(x0: int, y0: int, x1: int, y1: int, radius: int, color: tuple[int, int, int, int]) -> None:
        for y in range(y0, y1):
            for x in range(x0, x1):
                dx = max(x0 + radius - x, 0, x - (x1 - radius - 1))
                dy = max(y0 + radius - y, 0, y - (y1 - radius - 1))
                if dx * dx + dy * dy <= radius * radius:
                    set_px(x, y, color)

    def circle(cx: int, cy: int, r: int, color: tuple[int, int, int, int]) -> None:
        rr = r * r
        for y in range(cy - r, cy + r + 1):
            for x in range(cx - r, cx + r + 1):
                if (x - cx) * (x - cx) + (y - cy) * (y - cy) <= rr:
                    set_px(x, y, color)

    def triangle(points: list[tuple[int, int]], color: tuple[int, int, int, int]) -> None:
        min_x = min(p[0] for p in points)
        max_x = max(p[0] for p in points)
        min_y = min(p[1] for p in points)
        max_y = max(p[1] for p in points)

        def sign(p1: tuple[int, int], p2: tuple[int, int], p3: tuple[int, int]) -> int:
            return (p1[0] - p3[0]) * (p2[1] - p3[1]) - (p2[0] - p3[0]) * (p1[1] - p3[1])

        for y in range(min_y, max_y + 1):
            for x in range(min_x, max_x + 1):
                p = (x, y)
                d1 = sign(p, points[0], points[1])
                d2 = sign(p, points[1], points[2])
                d3 = sign(p, points[2], points[0])
                if not ((d1 < 0 or d2 < 0 or d3 < 0) and (d1 > 0 or d2 > 0 or d3 > 0)):
                    set_px(x, y, color)

    # Background.
    for y in range(size):
        for x in range(size):
            shade = int(20 + 20 * (x + y) / (2 * size))
            set_px(x, y, (shade, 42 + shade // 3, 48 + shade // 4, 255))

    rounded_rect(size // 12, size // 12, size - size // 12, size - size // 12, size // 7, (15, 118, 110, 255))
    rounded_rect(size // 6, size // 6, size - size // 6, size - size // 6, size // 10, (247, 247, 244, 255))
    rounded_rect(size // 4, size // 3, size * 3 // 5, size // 3 + size // 13, size // 30, (15, 118, 110, 255))
    rounded_rect(size // 4, size // 2 - size // 26, size * 3 // 5, size // 2 + size // 26, size // 30, (15, 118, 110, 255))
    rounded_rect(size // 4, size * 2 // 3 - size // 26, size * 3 // 5, size * 2 // 3 + size // 26, size // 30, (15, 118, 110, 255))
    circle(size * 2 // 3, size // 2, size // 7, (124, 45, 18, 255))
    triangle(
        [(size * 2 // 3 - size // 18, size // 2 - size // 12), (size * 2 // 3 - size // 18, size // 2 + size // 12), (size * 2 // 3 + size // 13, size // 2)],
        (247, 247, 244, 255),
    )
    return data


def png_from_rgba(size: int, pixels: bytes) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)

    raw = bytearray()
    stride = size * 4
    for y in range(size):
        raw.append(0)
        raw.extend(pixels[y * stride : (y + 1) * stride])
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b"")


def write_ico() -> None:
    images = []
    for size in (16, 32, 48, 256):
        images.append((size, png_from_rgba(size, rgba(size))))

    header = struct.pack("<HHH", 0, 1, len(images))
    directory = bytearray()
    offset = 6 + 16 * len(images)
    payload = bytearray()
    for size, png in images:
        width = 0 if size == 256 else size
        height = 0 if size == 256 else size
        directory.extend(struct.pack("<BBBBHHII", width, height, 0, 0, 1, 32, len(png), offset))
        payload.extend(png)
        offset += len(png)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(header + bytes(directory) + bytes(payload))
    PNG32_OUT.write_bytes(png_from_rgba(32, rgba(32)))
    print(OUT)
    print(PNG32_OUT)


if __name__ == "__main__":
    write_ico()

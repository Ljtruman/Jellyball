"""Recover MPEG-TS segments that a provider packed inside an image.

Some CDNs only accept images, so providers upload each HLS segment as a real
PNG whose pixel bytes carry the segment: the magic ``TIKTIKPX``, a 4-byte
big-endian payload length, then the gzip-compressed MPEG-TS. Unlike a fake
image header glued in front of plain TS (which ``ts_normalize.find_ts_start``
skips), the TS bytes here are compressed and PNG-filtered, so the image has
to be decoded.
"""

import io
import struct
import zlib
from typing import Optional

from ts_normalize import find_ts_start

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PIXEL_PAYLOAD_MAGIC = b"TIKTIKPX"
# A segment is a few MB; refuse anything that would decode or inflate to more.
MAX_DECODED_BYTES = 64 * 1024 * 1024
_CHANNELS = {0: 1, 2: 3, 4: 2, 6: 4}  # PNG colour type -> samples per pixel (8-bit)


def _png_pixel_bytes(width: int, height: int, bit_depth: int, colour_type: int) -> Optional[int]:
    channels = _CHANNELS.get(colour_type)
    if channels is None or bit_depth != 8 or width <= 0 or height <= 0:
        return None
    return width * height * channels


def unwrap_image_segment(data: bytes) -> Optional[bytes]:
    """The MPEG-TS packed inside a PNG segment, or None when `data` isn't one
    (callers then treat it as before)."""
    if not data.startswith(PNG_SIGNATURE) or data[12:16] != b"IHDR" or len(data) < 33:
        return None
    width, height, bit_depth, colour_type = struct.unpack(">IIBB", data[16:26])
    size = _png_pixel_bytes(width, height, bit_depth, colour_type)
    if size is None or size > MAX_DECODED_BYTES:
        return None
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            pixels = image.tobytes()
    except Exception:
        return None
    if not pixels.startswith(PIXEL_PAYLOAD_MAGIC) or len(pixels) < 12:
        return None
    (length,) = struct.unpack(">I", pixels[8:12])
    if length > len(pixels) - 12:
        return None
    try:
        inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)  # gzip framing
        ts = inflater.decompress(pixels[12:12 + length], MAX_DECODED_BYTES)
    except zlib.error:
        return None
    if inflater.unconsumed_tail or find_ts_start(ts) != 0:
        return None
    return ts

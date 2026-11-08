"""MPEG-TS segments packed inside PNG images (segment_unwrap).
The PNGs are built here from synthetic TS, never captured."""

import gzip
import io
import random
import struct
import unittest
import zlib

from PIL import Image

from segment_unwrap import MAX_DECODED_BYTES, PNG_SIGNATURE, PIXEL_PAYLOAD_MAGIC, unwrap_image_segment
from test_hls_session import make_ts_segment


def pack_png(payload: bytes, width: int = 64, mode: str = "RGB", magic: bytes = PIXEL_PAYLOAD_MAGIC,
             length: int = -1) -> bytes:
    """A real PNG whose pixel bytes carry magic + length + payload."""
    body = magic + struct.pack(">I", len(payload) if length < 0 else length) + payload
    channels = len(mode)
    row = width * channels
    # At least 32 rows of incompressible filler, like real ones: a tiny PNG
    # would pass find_ts_start by accident ("\x89PNG" holds a sync byte).
    height = max(32, -(-len(body) // row))
    body += random.Random(len(payload)).randbytes(height * row - len(body))
    image = Image.frombytes(mode, (width, height), body[:height * row])
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def ts_bytes(segments: int = 20) -> bytes:
    return b"".join(make_ts_segment(pts=90000 + i * 3000) for i in range(segments))


class UnwrapTests(unittest.TestCase):
    def test_recovers_the_packed_segment(self):
        ts = ts_bytes()
        for mode in ("RGB", "RGBA"):
            with self.subTest(mode=mode):
                self.assertEqual(unwrap_image_segment(pack_png(gzip.compress(ts), mode=mode)), ts)

    def test_other_data_is_left_alone(self):
        ts = ts_bytes()
        cases = {
            "plain ts": ts,
            "fake png header before plain ts": PNG_SIGNATURE + b"\x00" * 40 + ts,
            "ordinary picture": pack_png(b"\x00" * 3000, magic=b"NOTMAGIC"),
            "gzip of something else": pack_png(gzip.compress(b"<html>blocked</html>" * 50)),
            "raw ts without gzip": pack_png(ts),
            "length past the pixels": pack_png(gzip.compress(ts), length=10_000_000),
            "truncated png": pack_png(gzip.compress(ts))[:400],
            "empty": b"",
        }
        for name, data in cases.items():
            with self.subTest(name):
                self.assertIsNone(unwrap_image_segment(data))

    def test_oversized_image_is_refused_before_decoding(self):
        # 20000 x 20000 RGB would decode to 1.2 GB.
        ihdr = struct.pack(">IIBBBBB", 20000, 20000, 8, 2, 0, 0, 0)
        header = PNG_SIGNATURE + struct.pack(">I", 13) + b"IHDR" + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))
        self.assertIsNone(unwrap_image_segment(header + b"\x00" * 64))

    def test_inflation_is_capped(self):
        bomb = gzip.compress(b"\x47" + b"\x00" * (MAX_DECODED_BYTES + 1024))
        self.assertIsNone(unwrap_image_segment(pack_png(bomb, width=512)))


if __name__ == "__main__":
    unittest.main()

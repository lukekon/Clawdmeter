"""Screenshot a proto view over USB serial.

Usage: shot.py <out.png> [view-index] [wait-seconds]

Opens COM3 with DTR/RTS de-asserted (a plain open resets the ESP32 to
splash), sends `pview N`, then the `screenshot` serial cmd, and saves the
RGB565-LE framebuffer as PNG. Wire format (firmware/src/main.cpp
send_screenshot): a "SCREENSHOT_START <w> <h> <bytes>" line, then <bytes>
raw RGB565-LE, then "SCREENSHOT_END".
"""

import sys

import serial
from PIL import Image

PORT = "COM3"
BAUD = 115200


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else "shot.png"
    view = int(sys.argv[2]) if len(sys.argv) > 2 else 0

    ser = serial.Serial()
    ser.port = PORT
    ser.baudrate = BAUD
    ser.timeout = 10
    ser.dtr = False
    ser.rts = False
    ser.open()

    try:
        ser.reset_input_buffer()
        ser.write(f"pview {view}\n".encode())
        ser.flush()
        import time

        wait = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
        time.sleep(wait)  # let the view render a few animation frames
        ser.write(b"screenshot\n")
        ser.flush()

        # Skip chatter until the header line.
        while True:
            line = ser.readline().decode("ascii", "replace").strip()
            if line.startswith("SCREENSHOT_START"):
                _, w, h, size = line.split()
                w, h, size = int(w), int(h), int(size)
                break
            if "SCREENSHOT_UNSUPPORTED" in line or "SCREENSHOT_ERR" in line:
                print(line, file=sys.stderr)
                return 1

        buf = bytearray()
        while len(buf) < size:
            chunk = ser.read(size - len(buf))
            if not chunk:
                print(f"short read: {len(buf)}/{size}", file=sys.stderr)
                return 1
            buf += chunk

        # RGB565 little-endian -> RGB888.
        img = Image.new("RGB", (w, h))
        px = img.load()
        for y in range(h):
            row = y * w * 2
            for x in range(w):
                v = buf[row + x * 2] | (buf[row + x * 2 + 1] << 8)
                r = (v >> 11) & 0x1F
                g = (v >> 5) & 0x3F
                b = v & 0x1F
                px[x, y] = (r << 3 | r >> 2, g << 2 | g >> 4, b << 3 | b >> 2)
        img.save(out)
        print(f"saved {out} ({w}x{h})")
        return 0
    finally:
        ser.close()


if __name__ == "__main__":
    sys.exit(main())

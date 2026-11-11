"""
UT325F serial frame format.

Derived from a raw capture of a UNI-T UT325F on 2026-11-11 (155 frames across
three scenarios: all probes in degC, one probe unplugged, and the meter
switched to degF).

Frame: 56 bytes, emitted back-to-back with no gap.

  [ 0: 5]  5B  header AA 55 00 34 01
  [ 5:21] 16B  4 x float32 little-endian - the channel temperatures
  [21:25]  4B  per-channel status: 0x00 = reading valid, 0x30 = no probe
  [25:41] 16B  byte-identical mirror of [5:21] (155/155 frames)
  [41:45]  4B  mirror status - stays 0x00 even with a probe out, so it must
                NOT be used to decide validity
  [45:49]  4B  float32 LE - an additional value that tracks ambient a little
                above the channels; purpose unconfirmed, not exposed
  [49:53]  4B  status for that value (0x00 throughout the capture)
  [53:54]  1B  pad, 0x00 throughout
  [54:56]  2B  checksum: low 16 bits of the sum of bytes [0:54], big-endian

Two things worth knowing:

* TEMPERATURES ARE ALWAYS CELSIUS. Switching the meter's display to degF does
  not change the wire format: with the display reading 71.5/69.9/71.0/71.2 degF
  the stream carried 21.88/21.13/21.65/21.71, which is the same temperature in
  Celsius. Do not convert, and do not trust the meter's unit setting to tell
  you what is on the wire.
* The frame is 56 bytes. Earlier code here used 53, which happened to work only
  because every read re-scanned for the header and the channel data sits inside
  the first 53 bytes.
"""
import math
import struct

FRAME_HEADER = b'\xAA\x55\x00\x34\x01'
FRAME_SIZE = 56
CHECKSUM_SPAN = 54          # bytes covered by the trailing checksum

STATUS_OK = 0x00
STATUS_NO_PROBE = 0x30

_VALUES = slice(5, 21)
_STATUS = slice(21, 25)
_REFERENCE = slice(45, 49)
_CHECKSUM = slice(54, 56)


def frame_checksum(frame):
    """The checksum a frame should carry."""
    return sum(frame[:CHECKSUM_SPAN]) & 0xFFFF


def checksum_ok(frame):
    """True if the frame's trailing checksum matches its contents."""
    return (len(frame) >= FRAME_SIZE
            and frame_checksum(frame) == int.from_bytes(frame[_CHECKSUM], 'big'))


def find_frame(data):
    """Pull the first checksum-valid frame out of `data`.

    Returns (frame, remainder). A header whose frame fails the checksum is a
    false lock - skip one byte and keep looking, rather than consuming 56 bytes
    of someone else's data. When nothing is available, the remainder keeps only
    what could still become a frame, so the caller's buffer cannot grow without
    bound.
    """
    keep = len(FRAME_HEADER) - 1
    start = 0
    while True:
        i = data.find(FRAME_HEADER, start)
        if i == -1:
            return None, data[-keep:] if len(data) > keep else data
        if len(data) - i < FRAME_SIZE:
            return None, data[i:]               # incomplete: wait for more
        frame = data[i:i + FRAME_SIZE]
        if checksum_ok(frame):
            return frame, data[i + FRAME_SIZE:]
        start = i + 1                           # false lock: resync


def parse_frame(frame):
    """Four channel temperatures in Celsius; None where there is no reading.

    Assumes `frame` already passed the checksum (find_frame guarantees it).
    A status byte we have never seen is treated as "no reading" rather than
    guessed at, so an unknown device state cannot masquerade as a measurement.
    """
    temps = []
    status = frame[_STATUS]
    values = struct.unpack('<4f', frame[_VALUES])
    for i in range(4):
        if status[i] == STATUS_OK and math.isfinite(values[i]):
            temps.append(values[i])
        else:
            temps.append(None)
    return temps


def reference_value(frame):
    """The extra float at [45:49]. Purpose unconfirmed - informational only."""
    value, = struct.unpack('<f', frame[_REFERENCE])
    return value if math.isfinite(value) else None


def unknown_status_bytes(frame):
    """Status codes in this frame that the capture never showed."""
    return {b for b in frame[_STATUS] if b not in (STATUS_OK, STATUS_NO_PROBE)}

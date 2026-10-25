# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026, Matej Mician
"""Bosch GLM Bluetooth LE protocol: frames, checksum and decoding.

This module contains no Bluetooth code, so everything in it can be tested
without bleak or a meter. Observed on a GLM 50-27 CG.

Frame layouts:
    command, or frame the meter sends by itself:  C0 | command | length | data | CRC
    reply to a command:                           status | length | data | CRC
"""
from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import List, Optional, Union

logger = logging.getLogger(__name__)

# --- Bluetooth identifiers -------------------------------------------------

SERVICE_UUID = "02a6c0d0-0451-4000-b000-fb3210111989"
CHAR_UUID = "02a6c0d1-0451-4000-b000-fb3210111989"
DEVICE_NAME_UUID = "00002a00-0000-1000-8000-00805f9b34fb"  # standard GAP Device Name

# --- Command and frame codes -----------------------------------------------

START = 0xC0

CMD_HEARTBEAT = 0x11  # sent by the meter every 1.6-2.5 s; ignored
CMD_RAW_MEASURE = 0x40  # raw measurement, always from the top of the meter
CMD_SYNC = 0x55  # status request / enable push; also used by pushed events
CMD_MEASURE = 0x56  # button-style measurement

EVENT_STATUS = 0x02  # laser state changed
EVENT_RESULT = 0x06  # measurement result
EVENT_ERROR = 0xFE  # measurement failed

STATUS_OK = 0x00
STATUS_NAMES = {
    0x00: "ok",
    0x01: "communication timeout",
    0x03: "checksum error",
    0x04: "unknown command",
    0x05: "invalid access level",
    0x08: "hardware error",
    0x0A: "device not ready",
    0x10: "device not ready",
}

# Highest status byte we accept as the start of a reply. Real status codes are
# small; anything else that is not C0 means the byte stream is out of step.
_MAX_STATUS = 0x1F

# --- Distances ---------------------------------------------------------------

RAW_STEP_MM = Decimal("0.05")
REFERENCE_OFFSETS_MM = {
    "top": Decimal("0"),
    "bottom": Decimal("118.9"),  # measured at 0.5 m and 1.8 m: 118.65-119.00 mm
}


def crc8(data: bytes) -> int:
    """CRC-8: polynomial 0xA6, initial value 0xAA."""
    crc = 0xAA
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ 0xA6) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc


def build_command(command: int, data: bytes = b"") -> bytes:
    """Build a complete command frame, including length and CRC."""
    if not 0 <= command <= 0xFF:
        raise ValueError("command must be one byte")
    if len(data) > 0xFF:
        raise ValueError("data too long for one frame")
    frame = bytes([START, command, len(data)]) + bytes(data)
    return frame + bytes([crc8(frame)])


STATUS_REQUEST = build_command(CMD_SYNC, b"\x01\x00")  # C0 55 02 01 00 1A
MEASURE = build_command(CMD_MEASURE, b"\x00")  # C0 56 01 00 1E
RAW_MEASURE = build_command(CMD_RAW_MEASURE)  # C0 40 00 EE


# --- Parsed frames -----------------------------------------------------------


@dataclass(frozen=True)
class Reply:
    """The meter's answer to a command."""

    status: int
    data: bytes
    raw: bytes

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK


@dataclass(frozen=True)
class DeviceFrame:
    """A frame starting with C0 that the meter sent by itself."""

    command: int
    data: bytes
    raw: bytes


Frame = Union[Reply, DeviceFrame]


@dataclass(frozen=True)
class SyncData:
    """The common 16-byte payload of the status reply and pushed events."""

    kind: int  # EVENT_STATUS, EVENT_RESULT or EVENT_ERROR
    laser_on: bool
    counter: int
    value: float  # metres for EVENT_RESULT, 1.0 for EVENT_ERROR, else 0.0


class FrameParser:
    """Turns indication payloads into frames.

    One indication normally carries exactly one frame, but the parser also
    copes with several frames in one payload or a frame split over two.
    A frame with a bad checksum is dropped together with anything buffered
    after it, because the byte stream can no longer be trusted.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    def reset(self) -> None:
        self._buffer.clear()

    def feed(self, payload: bytes) -> List[Frame]:
        self._buffer += payload
        frames: List[Frame] = []
        buf = self._buffer
        while buf:
            if buf[0] == START:
                header = 3  # C0, command, length
            elif buf[0] <= _MAX_STATUS:
                header = 2  # status, length
            else:
                logger.warning("unexpected data from meter, discarding: %s", buf.hex(" "))
                buf.clear()
                break
            if len(buf) < header:
                break  # wait for the rest
            total = header + buf[header - 1] + 1
            if len(buf) < total:
                break  # wait for the rest
            raw = bytes(buf[:total])
            del buf[:total]
            if crc8(raw[:-1]) != raw[-1]:
                logger.warning("checksum error, discarding: %s", raw.hex(" "))
                buf.clear()
                break
            if header == 3:
                frames.append(DeviceFrame(command=raw[1], data=raw[3:-1], raw=raw))
            else:
                frames.append(Reply(status=raw[0], data=raw[2:-1], raw=raw))
        return frames


# --- Decoding ----------------------------------------------------------------


def parse_sync_data(data: bytes) -> Optional[SyncData]:
    """Decode the 16-byte payload of a 0x55 reply or event.

    Layout: kind, laser (01 on / 00 off), counter, 00, 4-byte float, 8 x 00.
    Returns None if the payload is too short.
    """
    if len(data) < 8:
        return None
    (value,) = struct.unpack_from("<f", data, 4)
    return SyncData(kind=data[0], laser_on=data[1] == 0x01, counter=data[2], value=value)


def parse_event(frame: DeviceFrame) -> Optional[SyncData]:
    """Decode a pushed 0x55 event frame; None for anything else."""
    if frame.command != CMD_SYNC:
        return None
    return parse_sync_data(frame.data)


def raw_distance_mm(data: bytes) -> Decimal:
    """Distance from a C0 40 reply: unsigned 32-bit little-endian x 0.05 mm.

    Measured from the top of the meter. 0 means the measurement failed.
    """
    if len(data) < 4:
        raise ValueError("raw measurement reply too short")
    (steps,) = struct.unpack_from("<I", data, 0)
    return steps * RAW_STEP_MM


def metres_to_mm(metres: float) -> Decimal:
    """Convert the float the meter sends (metres) to millimetres."""
    return Decimal(repr(metres)) * 1000


def round_mm(value: Union[Decimal, float]) -> float:
    """Round to 0.1 mm, halves rounding up (Python's round() would round to even)."""
    if not isinstance(value, Decimal):
        value = Decimal(repr(value))
    return float(value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))

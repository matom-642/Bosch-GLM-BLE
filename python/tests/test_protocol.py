# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026, Matej Mician
"""Tests for bosch_glm_ble.protocol, using frames captured from a GLM 50-27 CG.

Run from the python folder:  python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bosch_glm_ble import protocol as p  # noqa: E402


def h(text: str) -> bytes:
    """'C0-55-02 …' or 'c0 55 02 …' -> bytes."""
    return bytes.fromhex(text.replace("-", " "))


# Captured frames.
CAPTURED = [
    "C0-11-00-3A",
    "C0-55-02-01-00-1A",
    "C0-56-01-00-1E",
    "C0-40-00-EE",
    "00-00-82",
    "00-04-C6-8A-00-00-EC",
    "00-04-C7-8A-00-00-16",
    "00-04-2D-27-00-00-04",
    "00-04-00-00-00-00-5C",
    "00-10-02-01-4B-00-00-00-00-00-00-00-00-00-00-00-00-00-70",
    "00-10-02-00-4C-00-00-00-00-00-00-00-00-00-00-00-00-00-86",
    "C0-55-10-02-00-4B-00-00-00-00-00-00-00-00-00-00-00-00-00-5A",
    "C0-55-10-06-00-4C-00-A3-92-F2-3F-00-00-00-00-00-00-00-00-A0",
    "C0-55-10-06-00-54-00-8D-97-F2-3F-00-00-00-00-00-00-00-00-4A",
    "C0-55-10-FE-00-62-00-00-00-80-3F-00-00-00-00-00-00-00-00-50",
    "C0-55-02-F1-01-EE",
    "C0-55-02-F1-08-AC",
    "C0-41-00-96",
    "C0-42-00-1E",
    "C0-06-00-4A",
    "C0-04-00-BA",
]


class CrcTests(unittest.TestCase):
    def test_all_captured_frames_have_valid_crc(self):
        for text in CAPTURED:
            frame = h(text)
            with self.subTest(frame=text):
                self.assertEqual(p.crc8(frame[:-1]), frame[-1])

    def test_built_commands_match_known_bytes(self):
        self.assertEqual(p.STATUS_REQUEST, h("C0 55 02 01 00 1A"))
        self.assertEqual(p.MEASURE, h("C0 56 01 00 1E"))
        self.assertEqual(p.RAW_MEASURE, h("C0 40 00 EE"))

    def test_build_command_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            p.build_command(0x100)
        with self.assertRaises(ValueError):
            p.build_command(0x40, bytes(256))


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.parser = p.FrameParser()

    def test_reply(self):
        (frame,) = self.parser.feed(h("00-00-82"))
        self.assertIsInstance(frame, p.Reply)
        self.assertTrue(frame.ok)
        self.assertEqual(frame.data, b"")

    def test_device_frame(self):
        (frame,) = self.parser.feed(h("C0-11-00-3A"))
        self.assertIsInstance(frame, p.DeviceFrame)
        self.assertEqual(frame.command, p.CMD_HEARTBEAT)

    def test_two_frames_in_one_payload(self):
        frames = self.parser.feed(h("00-00-82") + h("C0-11-00-3A"))
        self.assertEqual([type(f) for f in frames], [p.Reply, p.DeviceFrame])

    def test_frame_split_over_two_payloads(self):
        frame = h("C0-55-10-06-00-54-00-8D-97-F2-3F-00-00-00-00-00-00-00-00-4A")
        self.assertEqual(self.parser.feed(frame[:5]), [])
        (parsed,) = self.parser.feed(frame[5:])
        self.assertEqual(parsed.raw, frame)

    def test_bad_crc_is_dropped(self):
        with self.assertLogs(p.logger, "WARNING"):
            self.assertEqual(self.parser.feed(h("C0-11-00-3B")), [])
        # parser recovers for the next frame
        self.assertEqual(len(self.parser.feed(h("C0-11-00-3A"))), 1)

    def test_garbage_is_dropped(self):
        with self.assertLogs(p.logger, "WARNING"):
            self.assertEqual(self.parser.feed(h("FF 01 02")), [])
        self.assertEqual(len(self.parser.feed(h("00-00-82"))), 1)


class DecodeTests(unittest.TestCase):
    def test_status_reply_laser_on(self):
        (reply,) = p.FrameParser().feed(
            h("00-10-02-01-4B-00-00-00-00-00-00-00-00-00-00-00-00-00-70"))
        data = p.parse_sync_data(reply.data)
        self.assertTrue(data.laser_on)
        self.assertEqual(data.counter, 0x4B)

    def test_result_event(self):
        (frame,) = p.FrameParser().feed(
            h("C0-55-10-06-00-54-00-8D-97-F2-3F-00-00-00-00-00-00-00-00-4A"))
        event = p.parse_event(frame)
        self.assertEqual(event.kind, p.EVENT_RESULT)
        self.assertFalse(event.laser_on)
        self.assertEqual(event.counter, 0x54)
        self.assertEqual(p.round_mm(p.metres_to_mm(event.value)), 1895.2)

    def test_error_event(self):
        (frame,) = p.FrameParser().feed(
            h("C0-55-10-FE-00-62-00-00-00-80-3F-00-00-00-00-00-00-00-00-50"))
        event = p.parse_event(frame)
        self.assertEqual(event.kind, p.EVENT_ERROR)
        self.assertEqual(event.value, 1.0)

    def test_heartbeat_is_not_an_event(self):
        (frame,) = p.FrameParser().feed(h("C0-11-00-3A"))
        self.assertIsNone(p.parse_event(frame))

    def test_raw_distance(self):
        self.assertEqual(p.raw_distance_mm(h("C6 8A 00 00")), Decimal("1776.30"))
        self.assertEqual(p.raw_distance_mm(h("00 00 00 00")), 0)

    def test_raw_distance_with_bottom_offset(self):
        mm = p.raw_distance_mm(h("C7 8A 00 00")) + p.REFERENCE_OFFSETS_MM["bottom"]
        self.assertEqual(p.round_mm(mm), 1895.3)  # 1895.25 rounds up

    def test_round_half_up(self):
        self.assertEqual(p.round_mm(Decimal("1776.35")), 1776.4)
        self.assertEqual(p.round_mm(Decimal("1776.25")), 1776.3)
        self.assertEqual(p.round_mm(501.45), 501.5)


if __name__ == "__main__":
    unittest.main()

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026, Matej Mician
"""Tests for the GLM class against a simulated meter (no Bluetooth needed).

The simulated meter answers the way the GLM 50-27 CG did, with shorter delays. 
Needs bleak installed, because client.py imports it.

Run from the python folder:  python -m unittest discover -s tests -v
"""
import asyncio
import os
import struct
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bosch_glm_ble import (  # noqa: E402
    GLM, GLMBusy, GLMDisconnected, GLMMeasurementError, GLMTimeout, protocol as p)


def reply(status: int, data: bytes = b"") -> bytes:
    frame = bytes([status, len(data)]) + data
    return frame + bytes([p.crc8(frame)])


def sync_data(kind: int, laser_on: bool, counter: int, value: float = 0.0) -> bytes:
    return bytes([kind, int(laser_on), counter, 0]) + struct.pack("<f", value) + bytes(8)


class FakeMeter:
    """Behaves like the meter on the other end of the characteristic."""

    def __init__(self):
        self.laser_on = False
        self.counter = 0x53
        self.distance_m = 1.8952499628067017  # what the meter reports, bottom reference
        self.raw_steps = 35526  # 1776.30 mm from the top
        self.can_measure = True
        self.result_delay = 0.05
        self.lose_acks = 0  # acknowledgements to swallow (command still runs)
        self.ignore_commands = 0  # commands to ignore completely
        self.writes = []
        self.client = None

    def push(self, frame: bytes, delay: float = 0.0):
        loop = asyncio.get_running_loop()
        loop.call_later(delay, lambda: self.client.indicate(frame))

    def ack(self, frame: bytes):
        if self.lose_acks:
            self.lose_acks -= 1
            return
        self.push(frame)

    def handle(self, data: bytes):
        self.writes.append(data)
        if self.ignore_commands:
            self.ignore_commands -= 1
            return
        if data == p.STATUS_REQUEST:
            self.ack(reply(0, sync_data(p.EVENT_STATUS, self.laser_on, self.counter)))
        elif data == p.MEASURE:
            self.ack(reply(0))
            if not self.laser_on:
                self.laser_on = True
                self.push(p.build_command(p.CMD_SYNC, sync_data(p.EVENT_STATUS, True, self.counter)), 0.01)
            else:
                self.laser_on = False
                if self.can_measure:
                    self.counter += 1
                    event = sync_data(p.EVENT_RESULT, False, self.counter, self.distance_m)
                else:
                    event = sync_data(p.EVENT_ERROR, False, self.counter, 1.0)
                self.push(p.build_command(p.CMD_SYNC, event), self.result_delay)
        elif data == p.RAW_MEASURE:
            steps = self.raw_steps if self.can_measure else 0
            self.ack(reply(0, struct.pack("<I", steps)))
        else:
            self.ack(reply(0x04))

    def press_button(self):
        """Simulate a single-mode button press: laser on, then a result."""
        self.counter += 1
        event = sync_data(p.EVENT_RESULT, False, self.counter, self.distance_m)
        self.push(p.build_command(p.CMD_SYNC, event))


class FakeBleakClient:
    def __init__(self, meter, address, disconnected_callback=None, timeout=10.0):
        self.meter = meter
        meter.client = self
        self.address = address
        self.disconnected_callback = disconnected_callback
        self.connected = False
        self.callback = None

    @property
    def is_connected(self):
        return self.connected

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        if self.connected:
            self.connected = False
            if self.disconnected_callback:
                self.disconnected_callback(self)

    async def start_notify(self, uuid, callback):
        self.callback = callback

    async def stop_notify(self, uuid):
        self.callback = None

    async def write_gatt_char(self, uuid, data, response=None):
        self.meter.handle(bytes(data))

    def indicate(self, frame: bytes):
        if self.connected and self.callback:
            self.callback(None, bytearray(frame))

    def drop_connection(self):
        """The meter switched itself off."""
        self.connected = False
        if self.disconnected_callback:
            self.disconnected_callback(self)


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.meter = FakeMeter()
        factory = lambda address, **kwargs: FakeBleakClient(self.meter, address, **kwargs)  # noqa: E731
        patcher = mock.patch("bosch_glm_ble.client.BleakClient", side_effect=factory)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.glm = GLM("14:9C:EF:02:97:D0")
        await self.glm.connect()

    async def asyncTearDown(self):
        await self.glm.disconnect()

    async def test_connect_reads_state(self):
        self.assertTrue(self.glm.is_connected)
        self.assertIs(self.glm.laser_on, False)
        self.assertEqual(self.meter.writes, [p.STATUS_REQUEST])

    async def test_measure_from_laser_off(self):
        reading = await self.glm.measure()
        self.assertEqual(reading.distance_mm, 1895.2)
        self.assertEqual(reading.source, "measure")
        self.assertEqual(reading.reference, "meter")
        self.assertEqual(reading.counter, 0x54)
        self.assertEqual(self.meter.writes.count(p.MEASURE), 2)
        self.assertIs(self.glm.laser_on, False)

    async def test_measure_with_laser_already_on(self):
        self.meter.laser_on = True
        await self.glm._request(p.STATUS_REQUEST)  # refresh the known state
        self.glm._apply_sync(sync_data(p.EVENT_STATUS, True, self.meter.counter))
        await self.glm.measure()
        self.assertEqual(self.meter.writes.count(p.MEASURE), 1)

    async def test_measure_failure(self):
        self.meter.can_measure = False
        with self.assertRaises(GLMMeasurementError):
            await self.glm.measure()

    async def test_measure_fast_bottom_and_top(self):
        reading = await self.glm.measure_fast()
        self.assertEqual(reading.distance_mm, 1895.2)  # 1776.30 + 118.9
        self.assertEqual((reading.source, reading.reference, reading.counter), ("fast", "bottom", None))
        self.glm.reference = "top"
        self.assertEqual((await self.glm.measure_fast()).distance_mm, 1776.3)

    async def test_measure_fast_failure(self):
        self.meter.can_measure = False
        with self.assertRaises(GLMMeasurementError):
            await self.glm.measure_fast()

    async def test_measure_fast_refuses_when_laser_on(self):
        self.glm._apply_sync(sync_data(p.EVENT_STATUS, True, 0))
        with self.assertRaises(GLMBusy):
            await self.glm.measure_fast()
        self.assertNotIn(p.RAW_MEASURE, self.meter.writes)

    async def test_stream(self):
        readings = [r async for r in self.glm.stream(count=3)]
        self.assertEqual(len(readings), 3)
        self.assertEqual(self.meter.writes.count(p.RAW_MEASURE), 3)

    async def test_button_press_goes_to_readings(self):
        self.meter.press_button()
        reading = await asyncio.wait_for(self.glm.readings().__anext__(), 1)
        self.assertEqual((reading.source, reading.distance_mm), ("button", 1895.2))

    async def test_lost_acknowledgement_does_not_double_step(self):
        self.meter.lose_acks = 1  # the laser-on command arrives, its ack doesn't
        reading = await self.glm.measure()
        self.assertEqual(reading.source, "measure")
        self.assertEqual(self.meter.writes.count(p.MEASURE), 2)

    async def test_ignored_command_is_resent(self):
        self.meter.ignore_commands = 1  # like the dropped C0 56 during testing
        reading = await self.glm.measure()
        self.assertEqual(reading.distance_mm, 1895.2)
        self.assertEqual(self.meter.writes.count(p.MEASURE), 3)

    async def test_no_answer_at_all_times_out(self):
        self.meter.ignore_commands = 10
        with self.assertRaises(GLMTimeout):
            await self.glm.measure_fast()

    async def test_disconnect_ends_readings(self):
        readings = self.glm.readings()
        asyncio.get_running_loop().call_later(0.05, self.meter.client.drop_connection)
        with self.assertRaises(GLMDisconnected):
            await asyncio.wait_for(readings.__anext__(), 1)
        self.assertFalse(self.glm.is_connected)
        with self.assertRaises(GLMDisconnected):
            await self.glm.measure_fast()

    async def test_disconnect_during_measure(self):
        self.meter.result_delay = 0.5
        asyncio.get_running_loop().call_later(0.2, self.meter.client.drop_connection)
        with self.assertRaises(GLMDisconnected):
            await self.glm.measure()

    async def test_heartbeat_is_ignored(self):
        self.meter.push(bytes.fromhex("C0 11 00 3A"))
        await asyncio.sleep(0.01)
        self.assertEqual((await self.glm.measure_fast()).distance_mm, 1895.2)


class KeepAliveTests(unittest.IsolatedAsyncioTestCase):
    async def test_keep_alive_sends_status_requests(self):
        meter = FakeMeter()
        factory = lambda address, **kwargs: FakeBleakClient(meter, address, **kwargs)  # noqa: E731
        with mock.patch("bosch_glm_ble.client.BleakClient", side_effect=factory):
            async with GLM("14:9C:EF:02:97:D0", keep_alive=0.05):
                await asyncio.sleep(0.18)
        self.assertGreaterEqual(meter.writes.count(p.STATUS_REQUEST), 3)  # connect + 2 or more


if __name__ == "__main__":
    unittest.main()

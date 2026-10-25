# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026, Matej Mician
"""Async client for Bosch GLM laser meters over Bluetooth LE.

    async with GLM as glm:
        reading = await glm.measure()
        print(reading.distance_mm)
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from dataclasses import dataclass
from typing import AsyncIterator, List, Optional, Tuple

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

from . import protocol as p

logger = logging.getLogger(__name__)

ACK_TIMEOUT = 1.0  # acknowledgement of a command
RAW_TIMEOUT = 2.0  # reply to C0 40 (normally 0.3 s)
ARM_TIMEOUT = 2.0  # status frame reporting the laser on
RESULT_TIMEOUT = 8.0  # result or error after C0 56 (a failure took 4.3 s)
RETRIES = 2  # extra attempts when an acknowledgement is lost


# --- Errors ------------------------------------------------------------------


class GLMError(Exception):
    """Base class for all errors from this module."""


class GLMNotFound(GLMError):
    """No meter was found, or the address did not answer."""


class GLMTimeout(GLMError):
    """A command got no acknowledgement or result in time, after retries."""


class GLMStatusError(GLMError):
    """The meter replied with a status other than OK."""

    def __init__(self, code: int) -> None:
        self.code = code
        self.name = p.STATUS_NAMES.get(code, "unknown status")
        super().__init__(f"meter replied with status 0x{code:02X} ({self.name})")


class GLMMeasurementError(GLMError):
    """The meter could not measure (no target, out of range)."""


class GLMBusy(GLMError):
    """A fast reading was requested while the meter's laser is on."""


class GLMDisconnected(GLMError):
    """The connection to the meter dropped or was never made."""


# --- Readings ----------------------------------------------------------------


@dataclass(frozen=True)
class Reading:
    ok: bool  # False: the meter could not measure (only from readings())
    distance_mm: Optional[float]  # rounded to 0.1 mm; None when ok is False
    source: str  # "measure", "fast" or "button"
    reference: str  # "meter" (the meter's own setting), "top" or "bottom"
    counter: Optional[int]  # the meter's measurement counter; None for fast readings
    timestamp: float  # time.time() when the frame arrived
    raw: bytes  # the frame the value came from


_DISCONNECTED = object()  # end-of-stream marker in the readings queue


# --- Client ------------------------------------------------------------------


class GLM:
    """A connection to one Bosch GLM meter.

    address:    the meter's Bluetooth address; None connects to the first meter found.
    reference:  "top" or "bottom"; used only for fast readings (C0 40), which the
                meter always measures from the top. measure() and button readings
                follow the reference plane set on the meter.
    timeout:    seconds allowed for connecting.
    keep_alive: seconds between status requests that stop the meter's 5-minute
                auto-off; None (default) leaves auto-off alone.
    """

    def __init__(
        self,
        address: Optional[str] = None,
        *,
        reference: str = "bottom",
        timeout: float = 20.0,
        keep_alive: Optional[float] = None,
    ) -> None:
        if reference not in p.REFERENCE_OFFSETS_MM:
            raise ValueError(f"reference must be one of {sorted(p.REFERENCE_OFFSETS_MM)}")
        if keep_alive is not None and not 0 < keep_alive < 300:
            raise ValueError("keep_alive must be between 0 and 300 seconds")
        self.address = address
        self.reference = reference
        self.timeout = timeout
        self.keep_alive = keep_alive

        self._client: Optional[BleakClient] = None
        self._connected = False
        self._parser = p.FrameParser()
        self._lock = asyncio.Lock()
        self._readings: asyncio.Queue = asyncio.Queue()
        self._reply_future: Optional[asyncio.Future] = None
        self._result_future: Optional[asyncio.Future] = None
        self._keep_alive_task: Optional[asyncio.Task] = None
        self._laser_on: Optional[bool] = None
        self._counter: Optional[int] = None

    # --- properties ---

    @property
    def laser_on(self) -> Optional[bool]:
        """Last known laser state; None before the first report."""
        return self._laser_on

    @property
    def is_connected(self) -> bool:
        return self._connected and self._client is not None and self._client.is_connected

    # --- discovery ---

    @staticmethod
    async def discover(timeout: float = 5.0, read_names: bool = True) -> List[Tuple[Optional[str], str]]:
        """Scan for meters. Returns a list of (name, address).

        Meters are recognised by their advertised service UUID, because they
        advertise no name. With read_names, each meter is briefly connected to
        read its name; the name is None if that fails.
        """
        found = await BleakScanner.discover(timeout=timeout, return_adv=True)
        meters = []
        for device, adv in found.values():
            if p.SERVICE_UUID in (uuid.lower() for uuid in adv.service_uuids):
                meters.append((adv.local_name or device.name, device))
        result = []
        for name, device in meters:
            if name is None and read_names:
                name = await _read_device_name(device)
            result.append((name, device.address))
        return result

    # --- connection ---

    async def __aenter__(self) -> "GLM":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.disconnect()

    async def connect(self) -> None:
        """Connect, subscribe to indications and enable measurement push."""
        if self.is_connected:
            return
        if self.address is None:
            meters = await self.discover(read_names=False)
            if not meters:
                raise GLMNotFound("no GLM meter found; is it switched on and not connected elsewhere?")
            self.address = meters[0][1]

        self._parser.reset()
        self._readings = asyncio.Queue()
        self._laser_on = None
        self._counter = None

        client = BleakClient(self.address, disconnected_callback=self._on_disconnect, timeout=self.timeout)
        logger.info("connecting to %s", self.address)
        try:
            await client.connect()
        except (BleakError, asyncio.TimeoutError, OSError) as e:
            raise GLMNotFound(f"could not connect to {self.address}: {e}") from e
        self._client = client
        self._connected = True
        try:
            await client.start_notify(p.CHAR_UUID, self._on_indication)
            async with self._lock:
                reply = await self._request(p.STATUS_REQUEST)
            self._apply_sync(reply.data)
        except BaseException:
            await self.disconnect()
            raise
        logger.info("connected; laser %s, counter %s", self._laser_on, self._counter)
        if self.keep_alive:
            self._keep_alive_task = asyncio.create_task(self._keep_alive_loop())

    async def disconnect(self) -> None:
        task, self._keep_alive_task = self._keep_alive_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        client, self._client = self._client, None
        if client is not None and client.is_connected:
            try:
                await client.stop_notify(p.CHAR_UUID)
            except (BleakError, OSError, ValueError):
                pass
            try:
                await client.disconnect()
            except (BleakError, OSError):
                pass
        self._mark_disconnected("disconnected from the meter")

    # --- measuring ---

    async def measure(self) -> Reading:
        """One reading via C0 56, exactly like pressing the button.

        Follows the meter's reference plane and updates its display. If the
        meter's own continuous mode is running, this ends it.
        """
        self._ensure_connected()
        async with self._lock:
            if self._laser_on is not True:
                await self._arm_laser()
            return await self._take_measurement()

    async def measure_fast(self) -> Reading:
        """One reading via C0 40 (about 0.3 s), with the reference offset added."""
        self._ensure_connected()
        async with self._lock:
            if self._laser_on:
                raise GLMBusy("the meter's laser is on; a fast reading now would put it into an error state")
            reply = await self._request(p.RAW_MEASURE, timeout=RAW_TIMEOUT)
        distance = p.raw_distance_mm(reply.data)
        if distance == 0:
            raise GLMMeasurementError("the meter could not measure (no target or out of range)")
        distance += p.REFERENCE_OFFSETS_MM[self.reference]
        return Reading(
            ok=True,
            distance_mm=p.round_mm(distance),
            source="fast",
            reference=self.reference,
            counter=None,
            timestamp=time.time(),
            raw=reply.raw,
        )

    async def stream(self, count: Optional[int] = None, interval: float = 0.0) -> AsyncIterator[Reading]:
        """Fast readings back to back. count=None runs until the loop is left.

        Any error ends the stream, including GLMBusy when the button is pressed.
        """
        taken = 0
        while count is None or taken < count:
            yield await self.measure_fast()
            taken += 1
            if interval > 0 and (count is None or taken < count):
                await asyncio.sleep(interval)

    async def readings(self) -> AsyncIterator[Reading]:
        """Readings from button presses on the meter, until cancelled or disconnected."""
        self._ensure_connected()
        while True:
            item = await self._readings.get()
            if item is _DISCONNECTED:
                raise GLMDisconnected("disconnected from the meter")
            yield item

    # --- internals: commands ---

    def _ensure_connected(self) -> None:
        if not self.is_connected:
            raise GLMDisconnected("not connected to the meter")

    async def _send(self, frame: bytes, timeout: float) -> Optional[p.Reply]:
        """Write one frame and wait for the reply; None if none came in time."""
        self._ensure_connected()
        future = asyncio.get_running_loop().create_future()
        self._reply_future = future
        try:
            logger.debug("-> %s", frame.hex(" "))
            await self._client.write_gatt_char(p.CHAR_UUID, frame, response=True)
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            return None
        except (BleakError, OSError) as e:
            if not self.is_connected:
                raise GLMDisconnected("disconnected from the meter") from e
            raise GLMError(f"could not send to the meter: {e}") from e
        finally:
            self._reply_future = None

    async def _request(self, frame: bytes, timeout: float = ACK_TIMEOUT) -> p.Reply:
        """Send a command that is safe to repeat, retrying if no reply comes."""
        for attempt in range(RETRIES + 1):
            reply = await self._send(frame, timeout)
            if reply is not None:
                if not reply.ok:
                    raise GLMStatusError(reply.status)
                return reply
            logger.debug("no reply to %s (attempt %d)", frame.hex(" "), attempt + 1)
        raise GLMTimeout(f"no reply to {frame.hex(' ')}")

    async def _arm_laser(self) -> None:
        """First C0 56: switch the laser on and wait until the meter reports it."""
        for attempt in range(RETRIES + 1):
            reply = await self._send(p.MEASURE, ACK_TIMEOUT)
            if reply is not None:
                if not reply.ok:
                    raise GLMStatusError(reply.status)
                if await self._wait_for_laser(True, ARM_TIMEOUT):
                    return
                raise GLMTimeout("the meter acknowledged but did not report the laser on")
            if self._laser_on:  # the acknowledgement was lost, the command was not
                return
            logger.debug("no acknowledgement for laser on (attempt %d)", attempt + 1)
        raise GLMTimeout("no acknowledgement for laser on")

    async def _take_measurement(self) -> Reading:
        """Second C0 56: measure and wait for the result or error frame."""
        future = asyncio.get_running_loop().create_future()
        self._result_future = future
        try:
            for attempt in range(RETRIES + 1):
                reply = await self._send(p.MEASURE, ACK_TIMEOUT)
                if reply is not None:
                    if not reply.ok:
                        raise GLMStatusError(reply.status)
                    break
                # The acknowledgement may be lost while the command arrived. Give a
                # result a moment before resending, so the laser isn't switched on
                # again by mistake.
                if await _finished_within(future, ACK_TIMEOUT) or self._laser_on is False:
                    break
                logger.debug("no acknowledgement for measure (attempt %d)", attempt + 1)
            else:
                raise GLMTimeout("no acknowledgement for measure")
            try:
                reading = await asyncio.wait_for(future, RESULT_TIMEOUT)
            except asyncio.TimeoutError:
                raise GLMTimeout("no measurement result from the meter") from None
            return dataclasses.replace(reading, source="measure")
        finally:
            self._result_future = None

    async def _wait_for_laser(self, state: bool, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while self._laser_on is not state:
            if time.monotonic() >= deadline:
                return False
            self._ensure_connected()
            await asyncio.sleep(0.02)
        return True

    async def _keep_alive_loop(self) -> None:
        while self.is_connected:
            await asyncio.sleep(self.keep_alive)
            if not self.is_connected:
                break
            try:
                async with self._lock:
                    reply = await self._request(p.STATUS_REQUEST)
                self._apply_sync(reply.data)
                logger.debug("keep-alive sent")
            except GLMDisconnected:
                break
            except GLMError as e:
                logger.warning("keep-alive request failed: %s", e)

    # --- internals: incoming data ---

    def _on_indication(self, _characteristic, data: bytearray) -> None:
        for frame in self._parser.feed(bytes(data)):
            logger.debug("<- %s", frame.raw.hex(" "))
            if isinstance(frame, p.Reply):
                future = self._reply_future
                if future is not None and not future.done():
                    future.set_result(frame)
                else:
                    logger.debug("reply nobody was waiting for: %s", frame.raw.hex(" "))
            elif frame.command == p.CMD_HEARTBEAT:
                continue
            else:
                event = p.parse_event(frame)
                if event is None:
                    logger.debug("unknown frame from meter: %s", frame.raw.hex(" "))
                else:
                    self._handle_event(event, frame.raw)

    def _handle_event(self, event: p.SyncData, raw: bytes) -> None:
        self._laser_on = event.laser_on
        self._counter = event.counter
        waiting = self._result_future if self._result_future and not self._result_future.done() else None
        if event.kind == p.EVENT_RESULT:
            reading = Reading(
                ok=True,
                distance_mm=p.round_mm(p.metres_to_mm(event.value)),
                source="button",
                reference="meter",
                counter=event.counter,
                timestamp=time.time(),
                raw=raw,
            )
            if waiting is not None:
                waiting.set_result(reading)
            else:
                self._readings.put_nowait(reading)
        elif event.kind == p.EVENT_ERROR:
            if waiting is not None:
                waiting.set_exception(
                    GLMMeasurementError("the meter could not measure (no target or out of range)"))
            else:
                # A failed button press: deliver it so readings() can report it.
                # Raising instead would end the caller's readings() loop.
                self._readings.put_nowait(Reading(
                    ok=False,
                    distance_mm=None,
                    source="button",
                    reference="meter",
                    counter=event.counter,
                    timestamp=time.time(),
                    raw=raw,
                ))

    def _apply_sync(self, data: bytes) -> None:
        sync = p.parse_sync_data(data)
        if sync is not None:
            self._laser_on = sync.laser_on
            self._counter = sync.counter

    def _on_disconnect(self, _client: BleakClient) -> None:
        logger.info("meter disconnected")
        self._mark_disconnected("the meter disconnected")

    def _mark_disconnected(self, reason: str) -> None:
        if not self._connected:
            return
        self._connected = False
        self._laser_on = None
        for future in (self._reply_future, self._result_future):
            if future is not None and not future.done():
                future.set_exception(GLMDisconnected(reason))
        self._readings.put_nowait(_DISCONNECTED)


async def _finished_within(future: asyncio.Future, timeout: float) -> bool:
    """Wait up to timeout for a future without consuming or cancelling it."""
    try:
        await asyncio.wait_for(asyncio.shield(future), timeout)
    except asyncio.TimeoutError:
        return False
    except Exception:
        return True  # finished with an error; the caller will see it
    return True


async def _read_device_name(device) -> Optional[str]:
    """Connect briefly and read the standard Device Name; None if that fails."""
    try:
        async with BleakClient(device, timeout=15.0) as client:
            raw = await client.read_gatt_char(p.DEVICE_NAME_UUID)
            return bytes(raw).decode("utf-8", "replace").strip("\x00 ") or None
    except (BleakError, asyncio.TimeoutError, OSError) as e:
        logger.debug("could not read the name of %s: %s", device.address, e)
        return None

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026, Matej Mician
"""Example: connect to a Bosch GLM meter and take readings three ways.

    python examples/example_glm.py            (from the python folder)
    python examples/example_glm.py --debug    (also print every Bluetooth frame)

The meter's address is kept in glm_devices.json next to this script. If the
file doesn't exist yet, the script scans once, lets you pick a meter if there
are several, and saves it. Delete the file to scan again.
"""
import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

# Make the package importable without installing it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bosch_glm_ble import GLM, GLMError, GLMMeasurementError  # noqa: E402

DEVICES_FILE = Path(__file__).with_name("glm_devices.json")


def load_devices() -> list:
    if not DEVICES_FILE.exists():
        return []
    with open(DEVICES_FILE, encoding="utf-8") as f:
        return json.load(f).get("devices", [])


def save_devices(devices: list) -> None:
    with open(DEVICES_FILE, "w", encoding="utf-8") as f:
        json.dump({"devices": devices}, f, indent=2)


async def pick_meter() -> dict:
    devices = load_devices()
    if devices:
        return devices[0]

    print("No saved meter, scanning (switch the meter on)...")
    found = await GLM.discover()
    if not found:
        sys.exit("No GLM meter found. Is it switched on and not connected to another program?")
    if len(found) == 1:
        name, address = found[0]
    else:
        for i, (name, address) in enumerate(found):
            print(f"[{i}] {name or 'unknown name'} - {address}")
        name, address = found[int(input("Select a meter: "))]

    device = {"name": name, "address": address}
    save_devices([device])
    print(f"Saved {name or address} to {DEVICES_FILE.name}")
    return device


async def main() -> None:
    meter = await pick_meter()
    print(f"Connecting to {meter.get('name') or 'GLM'} ({meter['address']})...")

    async with GLM(meter["address"], reference="bottom") as glm:
        print("Connected.\n")

        # 1. One reading, exactly like pressing the button.
        try:
            reading = await glm.measure()
            print(f"measure():      {reading.distance_mm:.1f} mm (meter's reference plane)")
        except GLMMeasurementError:
            print("measure():      the meter could not measure (no target?)")

        # 2. Ten fast readings, from the bottom of the meter.
        print("\nstream(count=10):")
        try:
            async for reading in glm.stream(count=10):
                print(f"  {reading.distance_mm:.1f} mm")
        except GLMMeasurementError:
            print("  the meter could not measure (no target?)")

        # 3. Readings from the measure button, until Ctrl+C.
        print("\nPress the measure button on the meter (Ctrl+C to stop)...")
        async for reading in glm.readings():
            print(f"  button: {reading.distance_mm:.1f} mm (counter {reading.counter})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--debug", action="store_true", help="print every Bluetooth frame")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )
    if args.debug:
        logging.getLogger("bleak").setLevel(logging.INFO)  # keep bleak's own chatter down

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
    except GLMError as e:
        sys.exit(f"Error: {e}")

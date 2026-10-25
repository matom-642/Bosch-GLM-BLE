# bosch-glm-ble

Read measurements from Bosch GLM laser meters (e.g. GLM 50-27 CG) over Bluetooth LE: either typed straight into any window, or from your own Python scripts through an importable module.

> Fork of [ketan/Bosch-GLM50C-Rangefinder](https://github.com/ketan/Bosch-GLM50C-Rangefinder) by Ketan Padegaonkar.
> Not affiliated with or endorsed by Bosch.

## Origin and credits

This project is based on [ketan/Bosch-GLM50C-Rangefinder](https://github.com/ketan/Bosch-GLM50C-Rangefinder), which first worked out how to read measurements from the GLM 50-27 CG over Bluetooth LE. His Python script and ESP32 firmware are included in this repository.

The importable module `bosch_glm_ble` is new code. Its protocol support builds on ketan's findings, on [philipptrenz/BOSCH-GLM-rangefinder](https://github.com/philipptrenz/BOSCH-GLM-rangefinder) (a Bluetooth Classic implementation for the older GLM 50 C / 100 C, whose class layout and command set inspired the module) and on the [CaveSurvey](https://github.com/lz1asl/CaveSurvey/issues/150). The remaining commands, frame formats and timings were captured and tested on a GLM 50-27 CG for this fork.

## Supported devices

| Device | Status |
|---|---|
| Bosch GLM 50-27 CG | Tested on Windows |

Other Bosch meters that use Bluetooth LE may work, but are untested.

## What's in this repository

| Path | Description |
|---|---|
| `python/main.py` | Ketan's script: connects to the meter and types each measurement (in millimetres, followed by Enter) into the active window, e.g. a spreadsheet cell. Works on Windows and macOS. |
| `python/bosch_glm_ble/` | Importable module: connect to the meter, trigger measurements, take fast readings and receive button presses from your own scripts. |
| `python/examples/example_glm.py` | Example script using the module. |
| `python/tests/` | Tests for the module. They need no meter. |
| `python/docs/code-flow.md` | Flowcharts of how the module works. |
| `esp32/` | Ketan's original ESP32 firmware, which connects to the meter and acts as a Bluetooth keyboard. Unchanged from upstream. |

## Install

Requires Python 3.10 or newer (tested with 3.13) and a PC with Bluetooth LE.

| What you want to use | Windows | macOS |
|---|---|---|
| The module only | `pip install bleak` | `pip install bleak` |
| `main.py` (typing into windows) | `pip install bleak pynput` | `pip install -r python/requirements.txt` |

`requirements.txt` currently includes macOS-only packages (`pyobjc-*`), so on Windows install the packages directly as shown.

The meter accepts only one Bluetooth connection at a time. 

## Using the module

The module is async, because the Bluetooth library [bleak](https://github.com/hbldh/bleak) is. Run your code inside `asyncio.run()` and `await` each call:

```python
import asyncio
from bosch_glm_ble import GLM, GLMMeasurementError

async def main():
    async with GLM("14:9C:EF:02:97:D0", reference="bottom") as glm:
        reading = await glm.measure()            # like pressing the button
        print(reading.distance_mm, "mm")

        async for reading in glm.stream(count=10):   # about 3 readings per second
            print(reading.distance_mm, "mm")

        async for reading in glm.readings():     # button presses, until Ctrl+C
            if reading.ok:
                print(reading.distance_mm, "mm")
            else:
                print("measurement failed")

asyncio.run(main())
```

Until the module is installable with pip, run your script from the `python` folder, or add that folder to `sys.path` as the example script does.

### Finding the meter

The meter advertises no name, so `GLM.discover()` finds it by its Bluetooth service and returns a list of `(name, address)` pairs. The name can be `None` on some systems. `GLM()` without an address connects to the first meter it finds.

The module never reads or writes files. To avoid scanning every time, store the address yourself. The example script keeps it in `examples/glm_devices.json`.

### Ways to measure

| Method | What it does | Reference plane | Speed |
|---|---|---|---|
| `await glm.measure()` | Exactly like pressing the measure button; the display shows the result | The one set on the meter | About 0.7 s (turns the laser on, then measures) |
| `await glm.measure_fast()` | Raw measurement; the display is not updated | `reference=` argument: `"top"` or `"bottom"` (default) | About 0.3 s |
| `glm.stream(count=None, interval=0.0)` | Repeated `measure_fast()` readings | Same as `measure_fast()` | About 3 per second |
| `glm.readings()` | Readings from button presses on the meter, including failed ones | The one set on the meter | As you press |

Fast readings are always measured from the top of the meter; the module adds 118.9 mm for `"bottom"`, measured on a GLM 50-27 CG.

### Readings

Every method returns or yields a `Reading`:

| Field | Meaning |
|---|---|
| `ok` | `True` for a measurement; `False` for a failed button press (only from `readings()`) |
| `distance_mm` | Distance in millimetres, rounded to 0.1 mm; `None` when `ok` is `False` |
| `source` | `"measure"`, `"fast"` or `"button"` |
| `reference` | `"meter"` (the meter's own setting), `"top"` or `"bottom"` |
| `counter` | The meter's measurement counter; `None` for fast readings |
| `timestamp` | When the reading arrived (`time.time()`) |
| `raw` | The Bluetooth frame the value came from |

### Errors

All errors are subclasses of `GLMError`, so `except GLMError:` catches every one of them.

| Error | When |
|---|---|
| `GLMNotFound` | No meter found, or the address doesn't answer |
| `GLMMeasurementError` | `measure()` or `measure_fast()` couldn't measure: no target or out of range |
| `GLMBusy` | `measure_fast()` or `stream()` while the meter's laser is on, e.g. after a button press. A fast reading then would put the meter into an error state |
| `GLMTimeout` | The meter didn't answer in time, even after retries |
| `GLMStatusError` | The meter rejected a command |
| `GLMDisconnected` | The connection dropped, e.g. the meter switched itself off. `stream()` and `readings()` end with this error |

### Auto-off

The meter switches itself off after 5 minutes without activity, even while connected. For longer sessions pass `keep_alive=240` to send a harmless status request every 4 minutes:

```python
async with GLM(address, keep_alive=240) as glm:
    ...
```

It is off by default to save the battery.

### Example and tests

From the `python` folder:

```
python examples/example_glm.py            # one measure(), ten fast readings, then button presses
python examples/example_glm.py --debug    # also prints every Bluetooth frame
python -m unittest discover -s tests -v
```

## Using main.py

1. Turn on the meter and enable Bluetooth on it.
2. Run the script:

   ```
   cd python
   python main.py
   ```

3. Wait for the connection sound, click into the window where the values should go (e.g. an Excel cell), and press the measure button on the meter.

A sound plays on connect and on disconnect. On Windows these are the system "Default Beep" and "Critical Stop" sounds.

| Option | Description |
|---|---|
| `--log-level debug` | Print the raw Bluetooth frames received from the meter. Useful for troubleshooting. Other levels: `info` (default), `warning`, `error`, `critical`. |

**Device address:** the first time the script finds a meter, it saves the meter's address in `python/config.json` and connects to that address directly on later runs. If you switch to a different meter, delete `config.json`.

## Known limitations

- **The meter's own continuous mode** doesn't send live values over Bluetooth; it only sends the value when you stop it with the button. Use `stream()` instead for continuous readings from your script.
- **Heartbeat:** the meter sends a frame (`C0 11 00 3A`) every 1.6–2.5 s whose expected answer is unknown. It is ignored; measuring works without answering it.
- **Only tested** with one GLM 50-27 CG on Windows.

## References

- [lz1asl/CaveSurvey#150](https://github.com/lz1asl/CaveSurvey/issues/150): the Bluetooth LE commands for measuring.
- [philipptrenz/BOSCH-GLM-rangefinder](https://github.com/philipptrenz/BOSCH-GLM-rangefinder): Bluetooth Classic implementation for the GLM 50 C / GLM 100 C.
- [Nordic DevZone: sniffing a Bosch laser tape](https://devzone.nordicsemi.com/f/nordic-q-a/80589/sniffing-a-bosch-laser-tape-2/337766)

## Licence

BSD 3-Clause, see [LICENSE](LICENSE).

- `main.py`, `esp32/` and the rest of the original repository: Copyright (c) 2026, Ketan Padegaonkar
- `bosch_glm_ble`, its example, tests and docs: Copyright (c) 2026, Matej Mician

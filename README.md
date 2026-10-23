# bosch-glm-ble

Read measurements from Bosch GLM laser meters (e.g. GLM 50-27 CG) over Bluetooth LE, with a Python class for controlling the meter.

> Fork of [ketan/Bosch-GLM50C-Rangefinder](https://github.com/ketan/Bosch-GLM50C-Rangefinder) by Ketan Padegaonkar.
> Not affiliated with or endorsed by Bosch.

## Origin and credits

This project is based on [ketan/Bosch-GLM50C-Rangefinder](https://github.com/ketan/Bosch-GLM50C-Rangefinder), which reverse-engineered the Bluetooth LE protocol of the GLM 50-27 CG and provides the original Python script and ESP32 firmware in this repository.

The planned importable module takes its class layout and command set from [philipptrenz/BOSCH-GLM-rangefinder](https://github.com/philipptrenz/BOSCH-GLM-rangefinder) (MIT licence). That project uses Bluetooth Classic, which the GLM 50-27 CG doesn't support, so only its structure and command definitions are reused, adapted to Bluetooth LE.

## Supported devices

| Device | Status |
|---|---|
| Bosch GLM 50-27 CG | Tested on Windows |

Other Bosch meters that use Bluetooth LE may work, but are untested.

## What's in this repository

| Path | Description |
|---|---|
| `python/main.py` | Connects to the meter and types each measurement (in millimetres, followed by Enter) into the active window, e.g. a spreadsheet cell. Works on Windows and macOS. |
| Importable module | **Planned / work in progress.** A Python class for connecting to the meter, triggering measurements and receiving readings from your own scripts. |
| `esp32/` | Ketan's original ESP32 firmware, which connects to the meter and acts as a Bluetooth keyboard. Unchanged from upstream. |

## Install

Requires Python 3 and a PC with Bluetooth LE.

**Windows:**

```
pip install bleak pynput
```

**macOS:**

```
pip install -r python/requirements.txt
```

`requirements.txt` currently includes macOS-only packages (`pyobjc-*`), so on Windows install the two packages directly as shown above.

## Usage

1. Turn on the meter and enable Bluetooth on it.
2. Run the script:

   ```
   cd python
   python main.py
   ```

3. Wait for the connection sound, click into the window where the values should go (e.g. an Excel cell), and press the measure button on the meter.

A sound plays on connect and on disconnect. On Windows these are the system "Default Beep" and "Critical Stop" sounds.

**Options:**

| Option | Description |
|---|---|
| `--log-level debug` | Print the raw Bluetooth frames received from the meter. Useful for troubleshooting. Other levels: `info` (default), `warning`, `error`, `critical`. |

**Device address:** the first time the script finds a meter, it saves the meter's address in `python/config.json` and connects to that address directly on later runs. If you switch to a different meter, delete `config.json`.


## References

- [lz1asl/CaveSurvey#150](https://github.com/lz1asl/CaveSurvey/issues/150): describes the commands that should be sent to the meter.
- [philipptrenz/BOSCH-GLM-rangefinder](https://github.com/philipptrenz/BOSCH-GLM-rangefinder): Bluetooth Classic implementation for the GLM 50 C / GLM 100 C.
- [Nordic DevZone: sniffing a Bosch laser tape](https://devzone.nordicsemi.com/f/nordic-q-a/80589/sniffing-a-bosch-laser-tape-2/337766)

## Licence

BSD 3-Clause, see [LICENSE](LICENSE).

Copyright (c) 2026, Ketan Padegaonkar
Copyright (c) 2026, Matej Mician (additions in this fork)


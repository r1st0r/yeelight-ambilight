# Yeelight Ambilight

Real-time Ambilight-style screen synchronization for the **Yeelight Monitor Light Bar Pro (lamp15)**.

This project captures the top portion of the screen and mirrors its colors to the monitor light bar, creating an effect similar to Philips Ambilight.

## Features

* Real-time screen color synchronization
* Designed for **Yeelight Monitor Light Bar Pro**
* Left / Right color zone support
* Adaptive color smoothing
* Automatic dark scene detection
* Physical background light power control
* Windows and macOS support
* Open source and fully local

## Demo

```text
Screen:
┌─────────────────────────────┐
│ 🔴 Red       🟢 Green       │
└─────────────────────────────┘

Light Bar:
🔴 Left Side | 🟢 Right Side
```

## Requirements

* Python 3.10+
* Yeelight Monitor Light Bar Pro
* LAN Control enabled
* Device connected to the same network as the computer

## Installation

Clone the repository:

```bash
git clone https://github.com/r1st0r/yeelight-ambilight.git
cd yeelight-ambilight
```

Create a virtual environment:

```bash
python -m venv .venv
```

Activate it:

### Windows

```powershell
.venv\Scripts\activate
```

### macOS / Linux

```bash
source .venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

## Finding Your Yeelight IP

Enable LAN Control in the Yeelight app.

Example:

```bash
nc -vz 192.168.0.228 55443
```

If the connection succeeds, update:

```python
YEELIGHT_IP = "192.168.0.228"
```

inside `yeelight_ambilight.py`.

## Usage

Run:

```bash
python yeelight_ambilight.py
```

Example output:

```text
==================================================
Yeelight Ambilight
==================================================
Yeelight IP: 192.168.0.228
Screen area: top 15%
Press Ctrl+C to stop.
```

## Configuration

The following parameters can be adjusted:

```python
TOP_PERCENT = 0.15
FPS = 30
BLACK_THRESHOLD = 7
SEGMENT_FPS = 9
BG_BRIGHTNESS = 60
```

| Setting         | Description                                |
| --------------- | ------------------------------------------ |
| TOP_PERCENT     | Percentage of the screen used for sampling |
| FPS             | Capture rate                               |
| BLACK_THRESHOLD | Dark scene detection threshold             |
| SEGMENT_FPS     | Maximum segment update rate                |
| BG_BRIGHTNESS   | Rear light brightness                      |

## Current Status

### Working

* Screen capture
* Color extraction
* Adaptive smoothing
* Background power control
* Yeelight LAN communication
* Left / Right segment rendering

### Experimental

* High-speed color transitions
* Segment synchronization latency
* Advanced sampling algorithms
* UDP segment control research

## Known Limitations

The Yeelight Monitor Light Bar Pro exposes a documented TCP API for segmented colors and a UDP real-time API for background lighting.

Because segmented color control is currently rate-limited through TCP, very rapid color transitions may not be as smooth as native Ambilight implementations.

Future versions will investigate undocumented real-time segment control methods.

## Roadmap

### v0.4

* [ ] Automatic Yeelight discovery (broadcast / LAN discovery)
* [ ] Remove hardcoded device IP requirement
* [ ] Automatic reconnection after DHCP IP changes
* [ ] Device information logging (model, firmware, IP)

### v0.5

* [ ] Improve Left / Right responsiveness
* [ ] Reduce color transition latency
* [ ] Better color sampling algorithm
* [ ] Adaptive dark-scene handling
* [ ] Configurable color profiles

### v0.6

* [ ] Multi-monitor support
* [ ] Config file (`config.json`)
* [ ] Device selection when multiple Yeelight devices are detected
* [ ] Per-monitor lighting zones

### v0.7

* [ ] Native Windows executable
* [ ] System tray integration
* [ ] Auto-start with Windows
* [ ] Background service mode

### Research

* [ ] Reverse engineer undocumented realtime segment protocol
* [ ] UDP-based segment updates
* [ ] Higher refresh rates (>20 FPS)
* [ ] Lower latency Ambilight mode
* [ ] Hardware acceleration experiments

### Long-Term

* [ ] GUI configuration tool
* [ ] Presets and profiles
* [ ] Plugin architecture
* [ ] Support for additional Yeelight devices
* [ ] Cross-device ambient ecosystem


## Disclaimer

This project is not affiliated with or endorsed by Xiaomi or Yeelight.

Use at your own risk.



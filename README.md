# Yeelight Ambilight

Ambilight-style screen synchronization for the **Yeelight Monitor Light Bar Pro (lamp15)**.

The program captures the top strip of your screen and mirrors its colors to the rear RGB light of the bar: the left half of the screen drives the left half of the light, the right half drives the right half. Everything runs locally over your LAN, with no cloud and no account.

> **Status: v0.4 (in testing).** Developed and tested on Windows 11 with a 3440×1440 monitor and a lamp15 on firmware 38. See [Tested setup](#tested-setup).

## Features

* Left / right color zones (`set_segment_rgb`)
* Automatic lamp discovery on the LAN, so no IP address has to be configured
* Brightness follows the scene, color follows the picture (see [How it works](#how-it-works))
* Adaptive smoothing: slow gradients stay smooth, hard scene cuts react immediately
* Dark scene handling with hysteresis, so the light switches off only after sustained darkness
* Real-time transport over the lamp's UDP session
* Optional control of the bar's front (desk) light
* Optional `config.json`
* Built-in diagnostics (`--debug`, `--save-strip`) and a separate `probe.py` tool

## Requirements

* Python 3.10+
* Yeelight Monitor Light Bar Pro (lamp15)
* **LAN Control enabled** for the lamp in the Yeelight app
* The computer and the lamp on the same network and subnet

## Installation

```bash
git clone https://github.com/r1st0r/yeelight-ambilight.git
cd yeelight-ambilight
python -m venv .venv
```

Activate the environment:

```powershell
# Windows
.venv\Scripts\activate
```

```bash
# macOS / Linux
source .venv/bin/activate
```

Install the dependencies:

```bash
pip install -r requirements.txt
```

Keep the project in a folder you can write to (for example under your user directory). A protected location such as `C:\Program Files` makes saving files like `--save-strip` fail.

## Finding the lamp

There is nothing to configure. On start the program searches the LAN for Yeelight devices (SSDP multicast on every network interface), picks the lamp15, and remembers its device `id` in `~/.yeelight_ambilight_lamp.json`. If the lamp later gets a new IP address from DHCP, it is found again by that `id`.

List what is visible on your network:

```bash
python yeelight_ambilight.py --discover
```

Example output:

```text
0x0000000012345678  lamp15  fw 38  192.168.0.50:55443  power=on
```

If discovery cannot see the lamp (multicast blocked, VPN adapters, a different subnet), pin the address manually:

```bash
python yeelight_ambilight.py --ip 192.168.0.50
```

With `--ip` the address is fixed and automatic re-discovery is switched off.

## Usage

```bash
python yeelight_ambilight.py
```

Example start-up output:

```text
=======================================================
Yeelight Ambilight v0.4
=======================================================
Lamp: lamp15 fw 38 192.168.0.50 id 0x0000000012345678
Capture 60 fps, lamp 30 fps, brightness 1-60
Press Ctrl+C to stop.

Screen 3440x1440, strip 216 px
```

Press `Ctrl+C` to stop. On exit the rear light is switched off.

### Command-line options

| Option | Description |
| --- | --- |
| `--discover` | List Yeelight devices on the LAN and exit |
| `--ip ADDRESS` | Use this lamp address, no discovery |
| `--config FILE` | Load settings from a JSON file |
| `--monitor N` | Monitor index to capture (`1` = primary) |
| `--send-fps N` | Frames per second sent to the lamp (default 30) |
| `--capture-fps N` | Screen capture rate (default 60) |
| `--bright-max N` | Maximum brightness of the rear light, 1–100 (default 60) |
| `--main-light leave\|on\|off` | Front (desk) light: leave as is (default), switch on, or switch off at start |
| `--debug` | Print statistics every 2 seconds (see [Diagnostics](#diagnostics)) |
| `--save-strip FILE` | Save the captured strip as an image every 2 seconds |

## Configuration

Settings can be given in a JSON file and loaded with `--config config.json`. Only the keys you want to change are needed:

```json
{
  "bright_max": 80,
  "dark_hold_s": 3.0,
  "main_light": "off"
}
```

Command-line options override the file. Unknown keys stop the program with an error.

| Key | Default | Description |
| --- | --- | --- |
| `ip` | `null` | Lamp address. `null` means automatic discovery |
| `monitor` | `1` | Monitor index (`mss` numbering, 1 = primary) |
| `top_percent` | `0.15` | Share of the screen height used for sampling |
| `capture_fps` | `60` | Screen capture rate |
| `send_fps` | `30` | Lamp frame rate (UDP) |
| `bright_fps` | `15` | Maximum brightness updates per second |
| `refresh_s` | `0.5` | Unchanged state is re-sent this often, to repair lost UDP packets |
| `center_overlap` | `0.08` | Overlap of the left and right zones around the middle |
| `bright_min` | `1` | Lowest brightness while the lamp is on |
| `bright_max` | `60` | Highest brightness |
| `bright_gamma` | `0.9` | Curve from scene brightness to lamp brightness |
| `bright_deadband` | `2` | Brightness changes smaller than this are ignored |
| `luma_mix` | `0.5` | Scene brightness: `0` = mean of both sides, `1` = brighter side |
| `sat_boost` | `1.1` | Saturation boost of the sampled colors |
| `chroma_min` | `10.0` | A side darker than this keeps its previous color |
| `tau_slow` | `0.15` | Color smoothing time for slow changes, in seconds |
| `tau_fast` | `0.02` | Color smoothing time for fast changes, in seconds |
| `cut_distance` | `60.0` | Color change that counts as a fast scene |
| `luma_tau_up` | `0.04` | Smoothing time when the scene gets brighter |
| `luma_tau_down` | `0.25` | Smoothing time when the scene gets darker |
| `luma_cut` | `70.0` | Brightness jump that bypasses smoothing |
| `black_threshold` | `7.0` | Scene brightness at or below this counts as black |
| `wake_threshold` | `12.0` | Scene brightness above this wakes the light again |
| `dark_hold_s` | `2.0` | Darkness must last this long before the light is switched off |
| `wake_frames` | `2` | Bright frames in a row needed to switch the light back on |
| `main_light` | `"leave"` | Front light: `leave`, `on` or `off` (applied once at start, not restored on exit) |

## How it works

The bar has **two independent lights**: a rear RGB light and a front (desk) white light. This program drives the rear light only, and can switch the front one with `--main-light`.

Measured on the lamp15, a segment color carries **hue and saturation only**: `0x010000` and `0xFF0000` both show full red, and gray shows as white. Brightness is a separate, global setting for the whole rear light. So each frame is split in two:

1. **Chroma:** the average color of the left and right zone, normalized to full brightness, sent with `set_segment_rgb`.
2. **Luma:** the brightness of the scene, mapped to the lamp's global brightness (`bg_set_bright`).

Both go to the lamp over a UDP session at up to 30 frames per second. TCP is used only for power on and off.

Pipeline:

```text
screen strip  ->  one resize to a 16x3 grid  ->  left / right zones
   ->  chroma + luma  ->  time-based smoothing  ->  UDP sender thread  ->  lamp
```

A single sender thread always sends the newest frame, so a slow network can drop frames but never builds up delay.

### Dark scenes

The lamp cannot show true black through segment colors: a `(0,0,0)` segment still leaves a dim glow, even at the lowest brightness. So black is handled by power:

* Dark scene: brightness drops to the minimum immediately and the last color is held.
* After `dark_hold_s` seconds of darkness the rear light is switched off.
* When the picture gets bright again the light switches back on.

The lamp itself needs about a second to visibly switch on or off, although it acknowledges the command within a few milliseconds. The hold time prevents flicker on scenes that alternate quickly between dark and bright.

## Diagnostics

```bash
python yeelight_ambilight.py --debug
```

Every 2 seconds one line is printed:

```text
capture  59.0 fps (11.5 ms/frame) | lamp  24.0/s | bright  42 | power on  | errors 0 | active top=118.0 | lamp power/bg_power/bg_bright/bg_rgb=on/on/42/16711680 L=(201, 40, 30) R=(35, 180, 60)
```

| Field | Meaning |
| --- | --- |
| `capture` | Capture rate and time per frame |
| `lamp` | Frames per second actually sent to the lamp |
| `bright`, `power` | What the program is commanding |
| `errors` | Network errors in the sender |
| `active` / `off` | State of the dark-scene logic |
| `top`, `L`, `R` | Measured brightness of the strip and the average color of each side |
| `lamp power/...` | What the lamp itself reports, queried over TCP. If it differs from what was commanded, something else is controlling the lamp |

`--save-strip strip.png` writes the strip the program actually sees, which is useful to check what is captured (browser toolbars, wrong monitor, and so on).

### probe.py

`probe.py` is a standalone tool (standard library only) for measuring how a lamp behaves. It does not touch the main program.

| Command | Purpose |
| --- | --- |
| `python probe.py discover` | Discovery on every interface, shows the lamp's supported methods |
| `python probe.py info` | Read the lamp's properties |
| `python probe.py rtt` | Round-trip time of TCP segment commands |
| `python probe.py segments` | Left / right behavior |
| `python probe.py gray` | Check that brightness is ignored in segment colors |
| `python probe.py dark` | Residual glow at different global brightness levels |
| `python probe.py udp` | Which commands the lamp accepts over UDP |
| `python probe.py udpfps --fps 30` | Sustained UDP rate (you watch the lamp) |
| `python probe.py wake` | Acknowledge time of power off and on |
| `python probe.py poweroff` | Power off behavior with TCP and with UDP in play |
| `python probe.py chroma` | What `udp_chroma_sess_new` returns |
| `python probe.py raw METHOD [PARAMS...]` | Send any single command, for example `raw set_power off sudden 0` |

Add `--ip ADDRESS` before the command to skip discovery. Session tokens printed by the tool are temporary, but do not publish them.

## Troubleshooting

**The lamp is not found.** Check that LAN Control is on in the Yeelight app, that the computer and the lamp share a subnet, and that your firewall allows Python on the private network. Disconnect VPNs or virtual adapters if discovery still fails, or use `--ip`.

**A bright white light stays on when the screen is black.** That is the bar's front (desk) light, which the program does not control by default. Use `--main-light off`, or switch it off with the app or the button.

**The rear light stays lit or turns on by itself.** Close other programs that can control the lamp (the Yeelight app, Yeelight Station, Razer Synapse, Home Assistant) and compare with the `lamp power/...` field in `--debug`.

**Colors look white or washed out.** Gray and desaturated scenes show as white, because a segment color carries only hue and saturation. Raise `sat_boost` if you want more color.

**Capture is a wrong area.** Run with `--save-strip strip.png` and look at the image. Use `--monitor N` for another display.

**macOS.** Screen capture needs the *Screen Recording* permission for your terminal.

## Limitations

* Brightness is global for the whole rear light. When one side of the screen is dark and the other is bright, the dark side shows its color at the shared brightness.
* The lamp takes about a second to visibly switch on or off.
* Only one monitor is captured at a time.
* The format of the lamp's `udp_chroma_sess_new` session is undocumented and is not used.
* Not every firmware has been tested. Everything here was measured on a lamp15 with firmware 38.

## Tested setup

* Yeelight Monitor Light Bar Pro, model lamp15, firmware 38
* Main program: Windows 11 Pro, 3440×1440 monitor at 144 Hz (capture about 59 fps, around 12 ms per frame)
* `probe.py`: Windows 11 and macOS
* The main program has not been verified on macOS or Linux yet

## Protocol notes (lamp15, firmware 38)

Measured on a real device; they may differ on other models or firmware.

* Discovery: SSDP-style multicast to `239.255.255.250:1982`. The reply carries `id`, `model`, `fw_ver` and the supported methods.
* TCP control on port 55443. Replies come as one JSON message per line, mixed with `props` notifications, so replies must be matched by `id`. A command takes about 17 ms to be acknowledged.
* UDP on port 55444. A session is created with `udp_sess_new`, and the returned token is added to every command. Only `udp_sess_new` and `udp_sess_keep_alive` are answered. The keep-alive is sent about every 10 seconds, and at most four sessions exist at a time.
* Accepted over UDP: `set_segment_rgb`, `bg_set_rgb`, `bg_set_bright`, `bg_set_power`.
* `set_segment_rgb` takes two integers, `[left, right]`, as `0xRRGGBB`. Magnitude is ignored (hue and saturation only).
* `bg_set_power` controls the rear light; `set_power` controls the front light. The `power` property looks like a combined flag that stays `on` while either light is on.
* `udp_chroma_sess_new` works over UDP only (it returns a normal session token); over TCP it answers "method not supported". What it is used for is not documented.
* The lamp has no `set_music` method.

The UDP session behavior is described in Yeelight's public *Inter-Operation Specification (UDP)* in the `Yeelight/Yeelight-Chroma-Connector` repository.

## Roadmap

### v0.4

* [x] Automatic Yeelight discovery (LAN multicast)
* [x] Remove the hardcoded device IP requirement
* [ ] Automatic reconnection after DHCP IP changes (implemented, not yet verified on a real address change)
* [x] Device information logging (model, firmware, IP)

### v0.5

* [x] Improve left / right responsiveness
* [ ] Reduce color transition latency (capture is about 12 ms per frame; end-to-end latency is not measured yet)
* [ ] Better color sampling algorithm (grid sampling with zone overlap is done)
* [ ] Adaptive dark-scene handling (implemented, final check pending)
* [ ] Configurable color profiles

### v0.6

* [x] Config file (`config.json`)
* [ ] Multi-monitor support (a single monitor can be selected with `--monitor`)
* [ ] Device selection when multiple Yeelight devices are detected
* [ ] Per-monitor lighting zones

### v0.7

* [ ] Native Windows executable
* [ ] System tray integration
* [ ] Auto-start with Windows
* [ ] Background service mode

#### Tray application (minimal GUI)

* [ ] Core refactor: ambilight engine controllable from outside the CLI (start / stop / status / log events)
* [ ] Taskbar (notification area) icon with a context menu
* [ ] Ambilight on / off
* [ ] Power on / off for each light separately: rear (RGB) and front (desk)
* [ ] Brightness control for each light using the lamp's native brightness commands (rear: acts as the maximum brightness while ambilight is running)
* [ ] Automatic reconnect after signal loss (Wi-Fi drop, lamp reboot, new DHCP address) with a connection status indicator in the tray
* [ ] Debug console window: live log (capture FPS, lamp frames/s, errors, real lamp state), copy and save log
* [ ] GUI toolkit decision (TBD)

### Research

* [x] UDP-based segment updates (`set_segment_rgb` works over the UDP session)
* [x] Higher refresh rates (30 frames per second works; 60 not tested yet)
* [ ] Format of the undocumented `udp_chroma_sess_new` realtime session (idea: capture the official app's traffic)
* [ ] Lower latency Ambilight mode
* [ ] Hardware acceleration experiments

### Long-Term

* [ ] GUI configuration tool (full settings window; the minimal controls live in the v0.7 tray application)
* [ ] Presets and profiles
* [ ] Plugin architecture
* [ ] Support for additional Yeelight devices
* [ ] Cross-device ambient ecosystem

## Disclaimer

This project is not affiliated with or endorsed by Xiaomi or Yeelight.

Use at your own risk.

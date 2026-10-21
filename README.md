# Yeelight Ambilight

Ambilight-style screen synchronization for the **Yeelight Monitor Light Bar Pro (lamp15)**.

The program captures the top strip of your screen and mirrors its colors to the rear RGB light of the bar: the left half of the screen drives the left half of the light, the right half drives the right half. Everything runs locally over your LAN, with no cloud and no account.

> **Status: 0.5.0 (in development).** The planned v0.4 and v0.5 work is in the code and will ship together as 0.5.0. Still to check on a real setup: one dark half of the screen and the end-to-end latency. The automatic reconnect after an address change is implemented, but its real-world check is postponed to v0.7. Developed and tested on Windows 11 with a 3440×1440 monitor and a lamp15 on firmware 38. See [Tested setup](#tested-setup).

## Features

* Left / right color zones (`set_segment_rgb`)
* Automatic lamp discovery on the LAN, so no IP address has to be configured
* A dark half of the screen darkens that half of the light, and a fully dark screen switches the light off after a short hold
* Adaptive smoothing: slow gradients stay smooth, hard scene cuts react immediately
* Real-time transport over the lamp's UDP session
* Optional control of the bar's front (desk) light
* Optional `config.json`
* Tray application (preview, Windows): an icon in the notification area with Ambilight on/off, front light on/off, start with Windows
* Built-in diagnostics (`--debug`, `--trace`, `--save-strip`) and a separate `probe.py` measuring tool

## Project files

| File | Purpose |
| --- | --- |
| `yeelight_ambilight.py` | Command-line program |
| `tray_app.py` | Tray application (preview) |
| `build_exe.bat` | Builds `dist\YeelightAmbilight.exe` with PyInstaller (Windows) |
| `panel.py` | Sliders window and debug console opened from the tray |
| `ambilight_core.py` | The engine: screen analysis, color pipeline, sender, and the controllable `Engine` class |
| `yeelight_lan.py` | Lamp discovery, TCP and UDP clients |
| `probe.py` | Measuring tool for the lamp's behavior |
| `latency_flash.py` | Red/blue flasher for latency tests with a phone camera |

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

## Tray application (preview, Windows)

Instead of a console window the program can live in the notification area:

```powershell
python tray_app.py          # with a console, good for the first try
pythonw tray_app.py         # no console window
```

The icon shows the state (the two halves follow the screen when it runs, gray when Ambilight is off, yellow while connecting, red when the lamp is not found). The menu:

| Item | What it does |
| --- | --- |
| Status | Running, connecting, lamp not found (it keeps retrying), error |
| Ambilight | On: the rear light follows the screen. Off: the rear light is switched off |
| Front light on / off | The bar's front (desk) light. Applied after a short pause of the color stream, because the lamp ignores such commands right after a color frame |
| Start with Windows | Adds or removes the program in the current user's login items (it starts without a console window) |
| Open log | Opens `%LOCALAPPDATA%\YeelightAmbilight\ambilight.log` |
| Quit | Stops the program and switches the rear light off (takes about a second and a half) |

When Windows shuts down, restarts or logs off, the tray application switches the rear light off (a hidden window listens for the end-of-session message; the log shows `[tray] Windows is ending the session`). Only one instance can run at a time. Options are the same as for the command line (`--config`, `--ip`, `--bright-max`, `--debug`, and so on). Brightness and color temperature sliders and a console window are planned (see the roadmap). This is a first version, not yet verified on a real Windows setup.

## Finding the lamp

There is nothing to configure. On start the program searches the LAN for Yeelight devices (SSDP multicast on every network interface), picks the lamp15, and remembers its device `id` in `~/.yeelight_ambilight_lamp.json`. If the lamp later gets a new IP address from DHCP, it is meant to be found again by that `id` (implemented, but not yet verified on a real address change).

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
Yeelight Ambilight 0.5.0-dev (r12 zone reach + session end)
=======================================================
Lamp: lamp15 fw 38 192.168.0.50 id 0x0000000012345678
Capture 60 fps, lamp 30 fps, brightness 60 (fixed)
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
| `--bright-max N` | Brightness of the rear light, 1–100 (default 60) |
| `--main-light leave\|on\|off` | Front (desk) light: leave as is (default), switch on, or switch off at start |
| `--debug` | Print statistics every 2 seconds (see [Diagnostics](#diagnostics)) |
| `--trace` | Log every command sent to the lamp and every reply (very verbose) |
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
| `refresh_s` | `0.5` | Unchanged state is re-sent this often, to repair lost UDP packets |
| `udp_gap_s` | `0.02` | Minimum time between two UDP datagrams |
| `lock_s` | `1.3` | Power-off waits this long after the last color frame, because the lamp ignores power and brightness commands for about a second (1.05 s measured) after a frame |
| `center_overlap` | `0.08` | The zones meet at the center of the captured monitor and overlap by this share of the width. `0` gives a hard split |
| `zone_reach` | `0.7` | How far each zone extends from the center, as a share of its half. The outer edges, where game HUDs and inventories live, are ignored. `1.0` samples the whole half |
| `bright_max` | `60` | Brightness of the rear light (see [How it works](#how-it-works)) |
| `sat_boost` | `1.1` | Saturation boost of the sampled colors |
| `chroma_min` | `10.0` | A side darker than this keeps its previous color |
| `tau_slow` | `0.15` | Color smoothing time for slow changes, in seconds |
| `tau_fast` | `0.02` | Color smoothing time for fast changes, in seconds |
| `cut_distance` | `60.0` | Color change that counts as a fast scene |
| `black_threshold` | `7.0` | Both sides at or below this brightness: the whole light switches off after `dark_hold_s` |
| `side_dark_threshold` | `7.0` | A single side at or below this brightness is shown dark. Raise it (for example to 25) to darken dim scenes |
| `side_wake_threshold` | `12.0` | A dark side lights up again above this (always kept at least 4 above `side_dark_threshold`) |
| `min_lit_fraction` | `0.2` | A side with less than this share of lit columns counts as dark, even if its average is not black |
| `lit_hysteresis` | `0.1` | A dark side needs this much more than `min_lit_fraction` to light up again |
| `wake_threshold` | `12.0` | A side above this brightness lights up again |
| `dark_hold_s` | `2.0` | Both sides must stay black this long before the light is switched off |
| `wake_frames` | `2` | Bright frames in a row needed to switch the light back on |
| `main_light` | `"leave"` | Front light: `leave`, `on` or `off` (applied once at start, not restored on exit) |

### Experimental: adaptive brightness

Off by default, because the lamp does not allow it while colors are streaming (see below). These keys only have an effect with `"adaptive_brightness": true`:

| Key | Default | Description |
| --- | --- | --- |
| `adaptive_brightness` | `false` | Follow the scene brightness with `bg_set_bright` |
| `bright_min` | `1` | Lowest brightness |
| `bright_gamma` | `0.9` | Curve from scene brightness to lamp brightness |
| `bright_deadband` | `2` | Brightness changes smaller than this are ignored |
| `bright_fps` | `15` | Maximum brightness updates per second |
| `luma_mix` | `0.5` | Scene brightness: `0` = mean of both sides, `1` = brighter side |
| `luma_tau_up` | `0.04` | Smoothing time when the scene gets brighter |
| `luma_tau_down` | `0.25` | Smoothing time when the scene gets darker |
| `luma_cut` | `70.0` | Brightness jump that bypasses smoothing |

## How it works

The bar has **two independent lights**: a rear RGB light and a front (desk) white light. This program drives the rear light only, and can switch the front one with `--main-light`.

What was measured on the lamp15 (firmware 38) shapes the whole design:

* A segment color carries **hue and saturation only**. `0x010000` and `0xFF0000` both show full red, and gray shows as white. This holds over TCP and over UDP.
* The one exception is `(0,0,0)`: that side goes dark (a very faint glow can remain). It works per side, so one half can be dark while the other is lit.
* The rear light's brightness is a separate, global setting (`bg_set_bright`), but the lamp **ignores it for about one second after every segment command**. While colors stream continuously, brightness cannot change. So the brightness (`bright_max`) is set once at start and again when the light wakes up from darkness.
* TCP commands are limited to about **60 per minute**, so colors cannot be streamed over TCP. The UDP session has no such limit and carries all frames. TCP is used only for power on and off.

So each frame is reduced to two colors: the average color of the left and the right zone, normalized to full value (with a slight saturation boost), or `(0,0,0)` for a zone that is black. They are sent with `set_segment_rgb` over UDP at up to 30 frames per second.

Pipeline:

```text
screen strip  ->  one resize to a 64x3 grid  ->  left / right zones (split at the monitor center)
   ->  color + blackout per side  ->  time-based smoothing  ->  UDP sender thread  ->  lamp
```

A single sender thread always sends the newest frame, so a slow network can drop frames but never builds up delay.

### Dark scenes

* A side is sent as `(0,0,0)` when it is black (its average brightness is at or below `side_dark_threshold`) **or** when only a small part of it is lit (less than `min_lit_fraction` of its width). The second rule matters because the lamp cannot dim a color: without it, a thin bright strip next to a black half would light the whole half at full brightness. A side lights up again only when clearly lit (above `side_wake_threshold` and `min_lit_fraction` + `lit_hysteresis`), so it does not flicker.
* A dark side is not completely off: `(0,0,0)` leaves a faint glow. Only switching the whole light off makes it fully dark.
* When both sides stay black (at or below `black_threshold`) for `dark_hold_s` seconds, the rear light is switched off completely. The lamp acknowledges a power command but ignores it for about a second after a color frame, so the program stops sending frames, waits `lock_s`, sends the command, and then asks the lamp whether it really switched off (it retries if not). Because of the wait, the light goes out roughly `dark_hold_s` seconds after the screen turns black.
* On exit the program does the same, so the light is really off after `Ctrl+C` (this takes about two seconds).
* When the picture gets bright again the light switches back on. The lamp needs about a second to visibly switch on or off, although it acknowledges the command within a few milliseconds.

## Diagnostics

```bash
python yeelight_ambilight.py --debug
```

Every 2 seconds one line is printed:

```text
capture  59.0 fps (11.5 ms/frame) | lamp  24.0/s | sw  18/ 35 ms | bright  60 | power on  | errors 0 | active top=118.0 | lamp power/bg_power/bg_bright/bg_rgb=on/on/60/16711680 lit=100%/85% L=(201, 40, 30) R=(35, 180, 60)
```

| Field | Meaning |
| --- | --- |
| `capture` | Capture rate and time per frame |
| `lamp` | Frames per second actually sent to the lamp |
| `sw` | Software latency in milliseconds, average / maximum over the last 2 seconds: from the start of the screen grab to the moment the changed frame is handed to the network |
| `bright`, `power` | What the program is commanding |
| `errors` | Network errors in the sender |
| `active` / `off` | State of the dark-scene logic |
| `top`, `L`, `R` | Measured brightness of the strip and the average color of each side |
| `lit` | Share of each side's width that is not black (left / right) |
| `lamp power/...` | What the lamp itself reports, queried over TCP every 5 seconds. If it differs from what was commanded, something else is controlling the lamp |

`--save-strip strip.png` writes the strip the program actually sees, which is useful to check what is captured (browser toolbars, wrong monitor, and so on). `--trace` prints every command and reply with timestamps.

### Measuring latency

`sw` in the debug line covers only the computer: screen grab, analysis, and the sender. The full path also includes Wi-Fi, the lamp itself, and the monitor. To measure the total, use `latency_flash.py` and a phone camera:

1. Start the program: `python yeelight_ambilight.py --debug`.
2. Start the flasher: `python latency_flash.py`. It fills the screen with red and blue, alternating every 2 seconds (`--interval` changes this), and `Esc` quits. Red and blue are used so the lamp never switches off.
3. Film in slow motion (240 fps if possible, at least 120 fps) with the top edge of the screen and the lamp's rear glow in the same shot.
4. Step through the video frame by frame and count the frames between the screen changing and the lamp following. Latency = frames / camera fps.
5. Repeat for 10 or more changes and take the median.

### probe.py

`probe.py` is a standalone tool (standard library only) for measuring how a lamp behaves. It does not touch the main program. Many of its tests send TCP commands, so run them one at a time and, after a long TCP test, wait a minute (see the quota above).

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
| `python probe.py udpmag` | Whether UDP segment colors honour their brightness |
| `python probe.py udpgap` | Whether brightness commands are applied right after a segment command |
| `python probe.py lock [--gaps 900,1100,1300]` | How long brightness commands are ignored after a segment command (choose the gaps to narrow it down) |
| `python probe.py frontlock` | Whether the front light's brightness is also ignored right after a segment command |
| `python probe.py dither [--fps 60] [--smooth 150]` | Whether alternating a color with `(0,0,0)` frames can emulate dimmer light (you judge brightness and flicker); `--smooth` adds a fade time to every frame (tried: still flickers) |
| `python probe.py frontct` | The front light's color temperature: accepted range and whether it is also ignored right after a segment command |
| `python probe.py brightstream` | Whether a brightness change survives when the UDP color stream resumes (the slider case) |
| `python probe.py segbright` | Which commands change brightness in segment mode |
| `python probe.py segmix` | Brightness routes after segments were sent over UDP |
| `python probe.py tcpsustain --fps 30` | A sustained TCP stream, to see the quota |
| `python probe.py wake` | Acknowledge time of power off and on |
| `python probe.py poweroff` | Power off behavior with TCP and with UDP in play |
| `python probe.py maintest` | Rear light control after the front light was switched off |
| `python probe.py chroma` | What `udp_chroma_sess_new` returns |
| `python probe.py raw METHOD [PARAMS...]` | Send any single command, for example `raw set_power off sudden 0` |

Add `--ip ADDRESS` before the command to skip discovery. Session tokens printed by the tool are temporary, but do not publish them.

## Troubleshooting

**The lamp is not found.** Check that LAN Control is on in the Yeelight app, that the computer and the lamp share a subnet, and that your firewall allows Python on the private network. Disconnect VPNs or virtual adapters if discovery still fails, or use `--ip`.

**A bright white light stays on when the screen is black.** That is the bar's front (desk) light, which the program does not control by default. Use `--main-light off`, or switch it off with the app or the button.

**The rear light does not switch off on a black screen.** The lamp ignores power commands for about a second after a color frame. The program waits for this and verifies the result; if it still fails you will see `the lamp still reports bg_power=on` in the console, and `--trace` shows the exact commands.

**The rear light stays lit or turns on by itself.** Close other programs that can control the lamp (the Yeelight app, Yeelight Station, Razer Synapse, Home Assistant) and compare with the `lamp power/...` field in `--debug`.

**Commands are rejected with `client quota exceeded`.** The lamp accepts about 60 TCP commands per minute. Wait a minute. The main program uses TCP only for power changes (and, with `--debug`, a state query every 5 seconds).

**Colors look white or washed out.** Gray and desaturated scenes show as white, because a segment color carries only hue and saturation. Raise `sat_boost` if you want more color.

**The brightness does not follow the scene (dark game scenes stay bright), and a rear brightness slider has no effect.** Measured with `probe.py brightstream`: every new segment color frame brings the rear light back to full brightness, and the value set with `bg_set_bright` (which the lamp reports correctly) only shows after the colors have stopped for about a second. So while Ambilight runs, the rear light is always at full brightness and `bright_max` has no visible effect. Raise `side_dark_threshold` (for example 25) so that the dimmest scenes switch a side dark instead. Emulating dimmer light by alternating color and dark frames was tried (`probe.py dither`) and makes the light flicker visibly at 30 and 60 frames per second; a variant with fades is still to be tried.

**A HUD in the corner of the screen (an inventory, a minimap) colors the light.** Lower `zone_reach` (for example 0.5): each zone then samples only that share of its half, counted from the center of the screen.

**Capture is a wrong area.** Run with `--save-strip strip.png` and look at the image. Use `--monitor N` for another display.

**macOS.** Screen capture needs the *Screen Recording* permission for your terminal.

## Limitations

* While colors stream the rear light is always at full brightness (a color frame resets it); it cannot be dimmed or follow the scene.
* A half of the screen is either shown in full brightness or, when it is black, switched dark. There are no dim levels in between.
* The lamp takes about a second to visibly switch on or off.
* Only one monitor is captured at a time.
* The format of the lamp's `udp_chroma_sess_new` session is undocumented and is not used.
* Not every firmware has been tested. Everything here was measured on a lamp15 with firmware 38.

## Tested setup

* Yeelight Monitor Light Bar Pro, model lamp15, firmware 38
* Main program: Windows 11 Pro, 3440×1440 monitor at 144 Hz (capture about 59 fps, around 12 ms per frame)
* `probe.py`: Windows 11 and macOS
* Verified on that setup: automatic discovery, left / right zones, slow and fast color changes, switching the light off after a black screen (the lamp confirms it) and back on, and switching off on exit
* Not verified yet: one half of the screen black while the other is lit, end-to-end latency, reconnect after a real address change (postponed to v0.7), and the main program on macOS or Linux

## Protocol notes (lamp15, firmware 38)

Measured on a real device; they may differ on other models or firmware.

* Discovery: SSDP-style multicast to `239.255.255.250:1982`. The reply carries `id`, `model`, `fw_ver` and the supported methods.
* TCP control on port 55443. Replies come as one JSON message per line, mixed with `props` notifications, so replies must be matched by `id`. A command takes about 10–70 ms to be acknowledged.
* **TCP quota:** about 60 commands per minute. After that the lamp answers `{"code":-1,"message":"client quota exceeded"}` to everything until the minute is over.
* UDP on port 55444. A session is created with `udp_sess_new`, and the returned token is added to every command. Only `udp_sess_new` and `udp_sess_keep_alive` are answered. The keep-alive is sent about every 10 seconds, and at most four sessions exist at a time. No quota was observed at 30 frames per second.
* Accepted over UDP: `set_segment_rgb`, `bg_set_rgb`, `bg_set_bright`, `bg_adjust_bright`, `bg_set_power`.
* `set_segment_rgb` takes two integers, `[left, right]`, as `0xRRGGBB`. Magnitude is ignored (hue and saturation only), except `0`, which turns that side dark. Extra parameters `"sudden", 0` are accepted and change nothing.
* After a segment command the lamp ignores brightness commands (`bg_set_bright`, TCP or UDP) for about one second: ignored at 0.9 s, sometimes applied at 1.0 s, always applied from 1.1 s. The front light's `set_bright` is ignored in the same way.
* The same quiet period is needed for **power commands**: `bg_set_power off` sent right after a color frame is acknowledged (`ok`) but has no effect, and the lamp keeps reporting `bg_power: on`. Always check the result with `get_prop`.
* A UDP `bg_set_rgb` leaves segment mode (the whole rear light shows one color again). `bg_set_scene` with `"color"` sets color and brightness in one command (tested over TCP).
* The front light's color temperature is set with `set_ct_abx` (`[kelvin, effect, duration]`). The accepted range is at least 2700–6500 K (2000 and 7000 are rejected with `invalid params`). It is ignored right after a segment command like brightness: not applied up to 0.8 s, applied at 1.5 s.
* `bg_set_power` controls the rear light; `set_power` controls the front light. The `power` property looks like a combined flag that stays `on` while either light is on.
* `udp_chroma_sess_new` works over UDP only (it returns a normal session token); over TCP it answers "method not supported". What it is used for is not documented.
* The lamp has no `set_music` method.

The UDP session behavior is described in Yeelight's public *Inter-Operation Specification (UDP)* in the `Yeelight/Yeelight-Chroma-Connector` repository.

## Roadmap

Current version: **0.5.0-dev**. The v0.4 and v0.5 items below ship together as release 0.5.0.

### v0.4

* [x] Automatic Yeelight discovery (LAN multicast)
* [x] Remove the hardcoded device IP requirement
* [x] Device information logging (model, firmware, IP)

### v0.5

* [x] Improve left / right responsiveness
* [ ] Reduce color transition latency (capture is about 12 ms per frame and `--debug` shows the software latency; the end-to-end value still has to be measured with `latency_flash.py`)
* [ ] Better color sampling algorithm (grid sampling with zone overlap is done)
* [x] Dark-scene handling: the light switches off after sustained black and back on (verified on the lamp)
* [ ] Per-side blackout when only one half of the screen is dark (implemented, lit-fraction rule added after a first test; recheck on the lamp)

### v0.6

* [x] Config file (`config.json`)
* [ ] Configurable color profiles
* [ ] Multi-monitor support (a single monitor can be selected with `--monitor`)
* [ ] Device selection when multiple Yeelight devices are detected
* [ ] Per-monitor lighting zones (zone boundaries relative to each monitor's own center, and the position of the light bar)

### v0.7

* [ ] Native Windows executable
* [ ] System tray integration
* [ ] Auto-start with Windows
* [ ] Background mode: start at Windows login, no console window (a real Windows service cannot capture the screen). Written, to be verified
* [ ] Automatic reconnection after DHCP IP changes (moved from v0.4: implemented in the engine, a real address change still has to be tried; the tray shows the connection status)

#### Tray application (minimal GUI)

* [x] Core refactor: ambilight engine controllable from outside the CLI (start / stop / status / log events)
* [ ] Taskbar (notification area) icon with a context menu (first version written, to be verified on Windows)
* [ ] Ambilight on / off (in the first version)
* [ ] Power on / off for each light separately: rear (RGB) and front (desk) (in the first version: the Ambilight switch for the rear light, on / off items for the front light)
* [ ] Sliders in a small popup window opened from the tray: brightness of the front light and the color temperature of the front light (warm to cold white, 2700–6500 K as measured with `probe.py frontct`)
* [ ] Slider values are applied when the slider is released (the tray menu itself cannot hold a slider). While colors stream, the lamp ignores brightness changes of both lights right after a color frame, so the color stream pauses for about a second and a half while the new value is applied; the color temperature needs the same pause (measured), and changes of several sliders can be applied in one pause
* [ ] Automatic reconnect after signal loss (Wi-Fi drop, lamp reboot, new DHCP address) with a connection status indicator in the tray
* [ ] Debug console window: live log (capture FPS, lamp frames/s, errors, real lamp state), copy and save log
* [ ] GUI toolkit decision (candidate: `pystray` for the tray icon and `tkinter` for the popup and console windows)

### Research

* [x] UDP-based segment updates (`set_segment_rgb` works over the UDP session)
* [x] Higher refresh rates (30 frames per second works; 60 not tested yet)
* [ ] A way to change the rear light's brightness while colors stream (the lamp ignores brightness for about 1 second after each segment command). Alternating the color with `(0,0,0)` frames (`probe.py dither`) flickers at 30 and 60 frames per second and does not give a steady dimmer light; a variant with fade times is still to be tried
* [ ] Format of the undocumented `udp_chroma_sess_new` realtime session (idea: capture the official app's traffic)
* [ ] Lower latency Ambilight mode
* [ ] Hardware acceleration experiments

### Long-Term

* [ ] GUI configuration tool (full settings window; the minimal controls live in the v0.7 tray application)
* [ ] Presets and profiles

## Disclaimer

This project is not affiliated with or endorsed by Xiaomi or Yeelight.

Use at your own risk.

# Changelog

## [0.5.0] - unreleased

The first release since 0.3.1. It ships the planned v0.4 and v0.5 work together.

### Added

* `front_off_on_exit` (default on): quitting the program or shutting Windows down switches the front light off together with the rear light.
* Tray application with a control window: front light and backlight switches, front brightness and color temperature, a live lamp picture, a debug console, autostart and lamp-off at Windows shutdown. `build_exe.bat` builds a single exe.
* Backlight mode *Solid color*: a static color from six presets or any color (picker, hex, R/G/B) with working brightness; *Ambilight* mode shows the live screen colors.
* Automatic lamp discovery (SSDP on every network interface). The lamp is remembered by its device `id`, so the hardcoded IP is gone.
* Re-discovery by `id` when the connection is lost or the lamp gets a new address. *Implemented; the check on a real address change is postponed to v0.7.*
* Real-time color transport over the lamp's UDP session, in a dedicated sender thread that always sends the newest frame.
* Per-side blackout: a black half of the screen darkens that half of the light, using `(0,0,0)`.
* Dark-side rule based on the lit share of a zone (`min_lit_fraction`, `lit_hysteresis`), so a thin bright strip next to a black half does not light the whole half.
* Power-off that respects the lamp's behavior: it waits until the color stream has been quiet, sends the command, and verifies with the lamp that the light is really off. The same on exit.
* Control of the bar's front (desk) light with `--main-light`.
* `config.json` (`--config`) and command-line options for the main settings.
* Diagnostics: `--debug` (including software latency `sw` and the lamp's own reported state), `--trace`, `--save-strip`.
* `probe.py`, a standalone tool for measuring lamp behavior, and `latency_flash.py` for end-to-end latency tests with a phone camera.
* `yeelight_lan.py`: discovery, TCP and UDP clients.
* `ambilight_core.py`: the engine as a controllable object (start, stop, Ambilight on/off, front light, brightness and color temperature sliders' backend), with queued one-off commands that wait for the lamp's quiet period.
* `zone_reach`: each zone samples only the inner part of its half, so HUDs in the screen corners do not color the light.
* `side_dark_threshold` and `side_wake_threshold`: a single side can be darkened at a higher brightness than the one that switches the whole light off.
* `tray_app.py`: a first version of the Windows tray application (Ambilight on/off, front light on/off, start with Windows, log file, single instance). *Preview, not yet verified on a real Windows setup.*

### Changed

* Screen analysis uses one BOX resize to a 64×3 grid instead of a per-pixel Python loop (about 12 ms per frame at 3440×1440, instead of roughly a hundred).
* Left and right zones are split at the center of the captured monitor, with a configurable overlap.
* Smoothing is time based, with a fast path for hard scene cuts.
* The rear light's brightness is fixed (`bright_max`). Following the scene is available as an experimental, off-by-default option, because the lamp ignores brightness commands for about a second after every color command.
* The README is rewritten around the measured behavior of the lamp.
* `yeelight_ambilight.py` is now a thin command-line wrapper around the engine; its behavior is unchanged.

### Fixed

* The tray application did not switch the lamp off when Windows shut down; it now listens for the end-of-session message. *Untested on a real shutdown.*
* The lamp rejects more than about 60 TCP commands per minute (`client quota exceeded`); colors no longer go over TCP.
* A power-off sent right after a color frame was acknowledged but ignored, so the light stayed on.
* TCP replies are matched by `id`; a slow reply no longer closes the connection.
* Saving a diagnostic image to a protected folder no longer crashes the program.

### Known issues

* One dark half of the screen on the latest zone logic and the end-to-end latency are still to be verified on a real setup.
* Reconnect after a real address change has not been tried yet (postponed to v0.7).
* The brightness of the rear light cannot follow the scene while colors stream.
* A dark half is not completely off (a faint glow remains); only switching the whole light off makes it fully dark.
* The main program has been verified on Windows only.
* The tray application and the engine's one-off commands were tested against a simulated lamp only.

## [0.3.1]

The previous state of the repository: a single script with a hardcoded lamp IP, color segments over TCP, and per-pixel Python averaging.

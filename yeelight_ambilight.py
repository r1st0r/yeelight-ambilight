#!/usr/bin/env python3
"""
Yeelight Ambilight - command line.

Screen-top sync for the Yeelight Monitor Light Bar Pro (lamp15). The work is done by the engine in
ambilight_core.py; this file only reads the options and prints the log. Run it with --help.
"""

import argparse
import sys

import yeelight_lan as lan
from ambilight_core import *  # noqa: F401,F403  (re-exported for scripts and tests)
from ambilight_core import BUILD, VERSION, Engine, LampUnavailable, load_config


def main() -> None:
    parser = argparse.ArgumentParser(description=f"Yeelight Ambilight {VERSION}")
    parser.add_argument("--ip", help="lamp IP (default: auto-discovery)")
    parser.add_argument("--config", help="JSON file with Config overrides")
    parser.add_argument("--monitor", type=int)
    parser.add_argument("--send-fps", type=int)
    parser.add_argument("--capture-fps", type=int)
    parser.add_argument("--bright-max", type=int)
    parser.add_argument("--main-light", choices=["leave", "on", "off"],
                        help="front (desk) light of the bar: leave (default), on or off; not restored on exit")
    parser.add_argument("--discover", action="store_true", help="list lamps and exit")
    parser.add_argument("--debug", action="store_true", help="print stats every 2 s")
    parser.add_argument("--trace", action="store_true",
                        help="log every command sent to the lamp and every reply (verbose)")
    parser.add_argument("--save-strip", metavar="PNG",
                        help="save the captured strip every 2 s (overwritten) to see what the script sees")
    args = parser.parse_args()

    if args.discover:
        for dev_id, d in lan.discover().items():
            print(f"{dev_id}  {d['model']}  fw {d['fw_ver']}  {d['ip']}:{d['port']}  power={d['power']}")
        return

    cfg = load_config(args)
    cfg.poll_state = args.debug
    lan.TRACE = args.trace

    print("=" * 55)
    print(f"Yeelight Ambilight {VERSION} ({BUILD})")
    print("=" * 55)
    engine = Engine(cfg, log=print)
    engine.debug = args.debug
    engine.save_strip = args.save_strip
    engine.hint = "Press Ctrl+C to stop.\n"
    try:
        engine.run(retry=False)
    except LampUnavailable as exc:
        sys.exit(f"Cannot reach the lamp: {exc}")
    except KeyboardInterrupt:
        print("\nStopped.")
        return
    print("Done.")


if __name__ == "__main__":
    main()

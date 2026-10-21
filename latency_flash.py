#!/usr/bin/env python3
"""
latency_flash.py - full-screen red / blue flasher for measuring the screen-to-lamp latency.

How to measure (about two minutes):
  1. Start the ambilight:           python yeelight_ambilight.py --debug
  2. Start this flasher:            python latency_flash.py            (Esc quits)
  3. Film with a phone in slow motion (240 fps if possible, 120 fps at least), so that both the
     top edge of the screen and the lamp's rear glow are in the shot.
  4. In the video, step frame by frame and count the frames between the screen turning
     red / blue and the lamp following. Latency = frames / camera fps.
  5. Do this for 10 or more changes and take the median.

Red and blue are used on purpose: neither is dark, so the lamp never switches off,
and the change is easy to see on video.
"""

import argparse
import sys
import time

try:
    import tkinter as tk
except ImportError:
    sys.exit("tkinter is not available in this Python. On Windows reinstall Python with the 'tcl/tk' option; "
             "on Linux install python3-tk.")

COLORS = [("red", "#ff0000"), ("blue", "#0000ff")]


def main() -> None:
    parser = argparse.ArgumentParser(description="Full-screen red/blue flasher for latency tests")
    parser.add_argument("--interval", type=float, default=2.0, help="seconds between color changes")
    args = parser.parse_args()

    root = tk.Tk()
    root.attributes("-fullscreen", True)
    root.configure(bg=COLORS[0][1], cursor="none")
    root.bind("<Escape>", lambda _event: root.destroy())
    state = {"i": 0}

    def flip() -> None:
        state["i"] ^= 1
        name, value = COLORS[state["i"]]
        root.configure(bg=value)
        print(f"{time.strftime('%H:%M:%S')}  screen -> {name}", flush=True)
        root.after(int(args.interval * 1000), flip)

    print("Flashing red / blue. Press Esc to quit.")
    root.after(int(args.interval * 1000), flip)
    root.mainloop()


if __name__ == "__main__":
    main()

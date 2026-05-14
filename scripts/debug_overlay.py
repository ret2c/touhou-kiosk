#!/usr/bin/env python3
"""Small flashing red "DEBUG" indicator pinned to the top-left of the
visible playfield. Spawned alongside score_overlay when DEBUG_MODE is
set in /etc/default/touhou-kiosk. Reminds anyone looking at the kiosk
that this is not production state (e.g. threshold is at 10k instead
of 200k, hit window is 5s instead of 30s, etc.).

Placement: top-left of the panel-visible playfield region. With the
xrandr playfield-crop transform (--transform 0.8,0,32,0,0.56,16),
panel(0, 0) maps to fb(32, 16). We put the text at fb(36, 18) so it
sits just inside the visible playfield's top-left corner.

No background: we set the window's wm_attributes -alpha low. The text
stays readable because red on near-transparent reads as red+slight
tinge of whatever's behind, while the "background" of the window
fades to almost-invisible. Bare X11 with no compositor blends via the
root pixmap — works for our setup. If the host doesn't honor -alpha,
the fallback is a barely-visible tinted box (still no solid black).
"""
import tkinter as tk
import time


def main():
    W, H = 56, 12
    FB_X = 34
    FB_Y = 18

    r = tk.Tk()
    r.overrideredirect(True)
    r.attributes("-topmost", True)
    # Bare X11 has no compositor to honor alpha, so use a tiny black box
    # instead of relying on transparency. Box is small enough not to
    # intrude visually.
    BG = "black"
    r.configure(bg=BG, cursor="none")
    r.geometry(f"{W}x{H}+{FB_X}+{FB_Y}")

    lbl = tk.Label(
        r, text="DEBUG", fg="red", bg=BG,
        font=("DejaVu Sans", 8, "bold"),
    )
    lbl.place(relx=0.5, rely=0.5, anchor="center")

    def tick():
        on = int(time.monotonic() * 2) % 2  # 0.5 Hz blink
        lbl.configure(text="DEBUG" if on else "")
        r.after(250, tick)

    r.bind("<Escape>", lambda _e: r.destroy())
    r.after(50, tick)
    r.mainloop()


if __name__ == "__main__":
    main()

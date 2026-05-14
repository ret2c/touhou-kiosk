#!/usr/bin/env python3
"""Debug log tail overlay — shows the last 5 lines of the kiosk's
watcher log, pinned to the bottom-left of the visible playfield.
Only spawned in DEBUG_MODE (see launch_th12.sh).

Black background, red text. Font 7pt (1px smaller than DEBUG flash's
8pt). Refreshes 1 Hz.

Placement: bottom-LEFT of the playfield-crop region (NOT bottom-right,
where TH12 items spawn). With xrandr --transform 0.8,0,32,0,0.56,16,
panel(x,y) <-> fb(0.8*x + 32, 0.56*y + 16). The playfield extends in
fb from y=16 (top) to y=464 (bottom). We anchor the box so its bottom
edge sits ~4 px above fb y=464.

Long log lines are right-truncated to fit window width.
"""
import os
import tkinter as tk

LOG_PATH = "/tmp/watcher.log"
LINES = 5
# shrunk font ~0.5x and stretched across full playfield
# width. Playfield in fb coords is (32,16)-(416,464);
# width = 384. Box fills width-2 with 1px margins on each side.
W, H = 380, 36
FB_X = 33
FB_Y = 464 - H - 4  # = 424
FONT_SIZE = 5      # tuned for panel stretch
LINE_H = 7
REFRESH_MS = 1000
MAX_LINE_CHARS = 110


def tail_lines(path, n):
    """Return the last n INTERESTING lines of path. Filters out the
    high-volume per-tick `score=...` lines (the watcher logs one of
    those per score change, which floods the overlay and crowds out
    the interesting stuff like reset triggers, helper invocations,
    etc.). Returns empty list on any read error."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    lines = [
        ln for ln in text.splitlines()
        if not ln.startswith("score=")
    ]
    return lines[-n:]


def main():
    r = tk.Tk()
    r.overrideredirect(True)
    r.attributes("-topmost", True)
    r.configure(bg="black", cursor="none")
    r.geometry(f"{W}x{H}+{FB_X}+{FB_Y}")

    # 5 Label widgets, one per line. Using labels (not Text) so we
    # don't get a scrollbar / cursor / focus weirdness.
    labels = []
    for i in range(LINES):
        lbl = tk.Label(
            r, text="", fg="red", bg="black",
            font=("DejaVu Sans Mono", FONT_SIZE, "normal"),
            anchor="w", justify="left",
        )
        lbl.place(x=2, y=1 + i * LINE_H, width=W - 4, height=LINE_H)
        labels.append(lbl)

    def tick():
        lines = tail_lines(LOG_PATH, LINES)
        # Right-pad to LINES so older slots get cleared
        while len(lines) < LINES:
            lines.insert(0, "")
        for lbl, line in zip(labels, lines):
            # Truncate to MAX_LINE_CHARS so we don't overflow the
            # window width. Watcher log lines occasionally are very
            # long (full pgrep cmdlines on supervisor restart).
            if len(line) > MAX_LINE_CHARS:
                line = line[: MAX_LINE_CHARS - 1] + "…"
            lbl.configure(text=line)
        r.after(REFRESH_MS, tick)

    r.bind("<Escape>", lambda _e: r.destroy())
    r.after(100, tick)
    r.mainloop()


if __name__ == "__main__":
    main()

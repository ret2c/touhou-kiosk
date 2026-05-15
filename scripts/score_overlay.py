#!/usr/bin/env python3
"""Tkinter overlay window showing live TH12 score above the game.

Reads from /proc/<pid>/mem at TH12's known score address, displays
score/stage/frame in a borderless always-on-top window pinned to the
top of the X screen. The HUD numbers should match in real time.

Run as root or with CAP_SYS_PTRACE.

Usage:
    sudo DISPLAY=:0 ./score_overlay.py
"""

import argparse
import os
import re
import struct
import sys
import time
import tkinter as tk

TH12 = {
    "exe":   "th12.exe",
    "score": 0x004B0C44,
    "stage": 0x004B0CB0,
    "diff":  0x004AEBD0,
    "power": 0x004B0C48,
    "frame": 0x004B0CBC,
}


def find_pid(exe):
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/comm") as f:
                if f.read().strip() != exe:
                    continue
            with open(f"/proc/{pid}/status") as f:
                state = ""
                for line in f:
                    if line.startswith("State:"):
                        state = line.split()[1]
                        break
            if state in ("Z", "X"):
                continue  # skip zombies / dead
            # also skip if we can't read maps (no image, defunct)
            try:
                with open(f"/proc/{pid}/maps") as f:
                    if not f.read(64):
                        continue
            except OSError:
                continue
            return int(pid)
        except OSError:
            pass
    return None


def find_image_base(pid, exe):
    try:
        with open(f"/proc/{pid}/maps") as f:
            for line in f:
                if exe.lower() not in line.lower():
                    continue
                m = re.match(r"([0-9a-f]+)-[0-9a-f]+ r-xp", line)
                if m:
                    return int(m.group(1), 16)
    except OSError:
        return None
    return None


class Overlay:
    # Frame counter must exceed this for us to call the game "really
    # playing" and switch from the Loading animation to live score/stage.
    # Lower than this we're either in menu (stage=0) or in the brief
    # window between case-11 stage init and the stage-switch DLL firing
    # (stage=1 frame=0 for a few hundred ms).
    # Bleedover only manifests under rapid back-to-back auto-test
    # switching, not natural play-to-10k tempo. Brief 60-frame (~1s)
    # mask — just enough to hide the stage-init frame=0 noise, not
    # enough to feel like a long pause between transitions.
    LOADING_FRAME_THRESHOLD = 60
    LOADING_DOT_PERIOD_MS = 600  # one dot per 600ms
    # When this file exists, the watcher is currently firing a HIT flash
    # overlay (centered on the playfield). We hide ourselves entirely so
    # both overlays don't stack on top of each other.
    HIT_FLAG_FILE = "/tmp/hit_active"
    # Written by the watcher when do_reset() fires. Forces us into load
    # mode (full-playfield mask) IMMEDIATELY — before the visible reset
    # actually starts — so the user never sees the bleedover from the
    # old wine framebuffer. We clear it ourselves once we observe a
    # healthy gameplay state (stage in 1..7 AND frame past the threshold).
    RESETTING_FLAG_FILE = "/tmp/touhou_resetting"

    def __init__(self):
        self.cfg = TH12
        self.pid = None
        self.delta = 0
        self.fd = None
        self._loading_dots = 0
        self._loading_last_bump = 0.0
        self._hit_was_active = False
        self._open()

        # Two separate Toplevel windows rather than resizing one root.
        # The resize approach left ghost text pixels at the previous
        # position when the window moved between play (top, 80x25) and
        # load (center, 120x38) geometries: bare X with overrideredirect
        # gives no automatic compositor invalidation, so the OLD area
        # kept showing the previous canvas contents until wine repainted
        # over it on its next frame. Using withdraw/deiconify generates
        # proper X expose events that the wine vdesk repaints under.
        # The xrandr playfield-crop transform stretches fb vertically by
        # 1.79x (448 fb pixels → 800 panel pixels) and horizontally by
        # 1.25x. fb-side fonts at size 7 became nearly invisible on the
        # panel even though they look reasonable in scrot. Sizing both
        # fonts for panel-side readability.
        self.font_play = ("DejaVu Sans", 8, "bold")
        self.font_load = ("DejaVu Sans", 16, "bold")
        self.outline_w = 1

        # Live score readout: top-center of the visible playfield, pushed
        # below the very top edge so the non-uniform stretch doesn't shove
        # it into the panel bezel. Score-only (game intros its own stage).
        self.play_w = 78
        self.play_h = 20
        play_cx_fb, play_cy_fb = 224, 16 + self.play_h // 2 + 4

        # Loading mask: covers the ENTIRE wine vdesk (640x480), NOT just
        # the visible playfield. Bigger-than-visible on purpose: the X
        # server's PageFlip-eligibility check looks at whether the wine
        # vdesk window is unobscured at the X level. If load_win only
        # covers the playfield sub-region, the rest of wine's 640x480
        # is still visible to X → X happily page-flips wine's DMA-BUF
        # directly to scanout, divorcing the panel scanout from our
        # primary-fb mask. When wine then dies, the CRTC keeps holding
        # the now-orphan DMA-BUF and the panel shows stale wine pixels
        # while scrot (reading X primary fb) shows our clean mask.
        #
        # By making load_win span the full wine vdesk, X considers wine
        # fully obscured → PageFlip is no longer eligible → CRTC reads
        # from primary fb (which has our mask). Only the inner 384x448
        # is visible to the user (xrandr transform crops the rest off
        # the panel), but the outer ring exists in X-land to defeat the
        # page-flip optimization. 
        # DEBUG MODE: small centered box, not full-screen — so bleedover
        # is observable to the operator. The full-vdesk mask was useful
        # for production hiding, but during debug we want to SEE the
        # bleed to know if a fix worked.
        self.load_w = 140
        self.load_h = 48
        load_cx_fb, load_cy_fb = 224, 240

        # tk requires one tk.Tk for the mainloop; the visible windows
        # are tk.Toplevel children. Keep root hidden so it never paints.
        self.root = tk.Tk()
        self.root.withdraw()

        self.play_win, self.play_canvas = self._make_overlay(
            self.play_w, self.play_h, play_cx_fb, play_cy_fb)
        self.load_win, self.load_canvas = self._make_overlay(
            self.load_w, self.load_h, load_cx_fb, load_cy_fb)
        # Start with both off-screen — first _tick picks the right one.
        self._current_mode = None  # None | "play" | "load" | "hidden"
        # Move them off-screen *now* before mainloop starts so they
        # never briefly flash at their configured positions.
        self.root.after_idle(lambda: self._hide(self.play_win))
        self.root.after_idle(lambda: self._hide(self.load_win))
        # Reset-cycle tracking: after the watcher writes the resetting
        # flag, we need to keep the mask up until we've seen the game
        # cycle through low frame counter (stage_switch took effect)
        # AND back to high (new gameplay healthy). Without this, the
        # OLD stage's still-high frame counter looks like "playing"
        # immediately and we'd clear the flag before the visible
        # disruption even starts.
        self._observed_post_reset_low_frame = False

        # Clean stale reset flag from a previous kiosk run that crashed
        # mid-reset. Without this, a stale flag forces us into load mode
        # permanently on startup.
        try:
            os.unlink(self.RESETTING_FLAG_FILE)
        except OSError:
            pass

        self.root.bind("<Escape>", lambda _e: self.root.destroy())
        self.root.after(100, self._tick)

    def _make_overlay(self, w, h, cx_fb, cy_fb):
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg="black", cursor="none")
        ox = cx_fb - w // 2
        oy = cy_fb - h // 2
        # Stash the geometry string on the widget so _set_mode can re-
        # apply it after every deiconify. Tk's withdraw/deiconify cycle
        # on overrideredirect Toplevels loses position state otherwise:
        # the first window we deiconify keeps its requested geometry,
        # but any subsequent withdraw → ... → deiconify lands at (0,0).
        geom = f"{w}x{h}+{ox}+{oy}"
        win.geometry(geom)
        win._kiosk_geom = geom
        canvas = tk.Canvas(
            win, width=w, height=h, bg="black",
            highlightthickness=0, bd=0,
        )
        canvas.pack(fill="both", expand=True)
        return win, canvas

    # Sink position for hidden windows. Off-screen (negative coords).
    # Moving instead of withdraw/deiconify because tk's withdraw cycle
    # was losing position state on overrideredirect Toplevels in bare X:
    # after the first withdraw, subsequent deiconify lands at (0,0)
    # regardless of any geometry() call. Moving the window off-screen
    # achieves the same visual effect (invisible) without dropping
    # position state.
    HIDE_OFFSET = -10000

    def _show(self, win):
        win.geometry(win._kiosk_geom)
        win.lift()

    def _hide(self, win):
        # Parse geom "WxH+X+Y" → "WxH+(HIDE)+(HIDE)"
        geom = win._kiosk_geom
        w_h = geom.split("+", 1)[0]
        win.geometry(f"{w_h}+{self.HIDE_OFFSET}+{self.HIDE_OFFSET}")

    def _set_mode(self, mode: str) -> None:
        """Show exactly one overlay window (or hide both).
        modes: 'play' | 'load' | 'hidden'. Idempotent on no-change."""
        if mode == self._current_mode:
            return
        if mode == "play":
            self._hide(self.load_win)
            self._show(self.play_win)
        elif mode == "load":
            self._hide(self.play_win)
            self._show(self.load_win)
        else:
            self._hide(self.play_win)
            self._hide(self.load_win)
        self._current_mode = mode

    def _draw(self, score: int, stage: int, frame: int) -> None:
        # Hit flash is centered on the playfield; we want IT to be the
        # visual focus but we ALSO want load_win up as a wine-obscurer
        # so the X server can't page-flip wine's DMA-BUF to scanout
        # (which would survive past the reset and bleed through on the
        # panel). Compromise: keep load_win up at full 640x480 but
        # draw no text — pure black mask. Hit flash sits on top.
        #
        # Stale-flag guard: hit window is at most ~5s. If the flag is
        # older than 10s, the flash subprocess was killed without its
        # atexit cleanup firing — treat as no-hit and unlink the flag
        # so we don't sit in blank-mask mode forever.
        if os.path.exists(self.HIT_FLAG_FILE):
            try:
                age = time.time() - os.path.getmtime(self.HIT_FLAG_FILE)
            except OSError:
                age = 0
            if age > 10:
                try:
                    os.unlink(self.HIT_FLAG_FILE)
                except OSError:
                    pass
                self._hit_was_active = False
            else:
                self._set_mode("load")
                self.load_canvas.delete("all")
                self._hit_was_active = True
                return
        else:
            self._hit_was_active = False

        # "Real playing" means: stage in 1..7 AND frame has advanced
        # past the early-stage-init noise. During cold boot or right
        # after a stage_switch, frame ticks from 0 up; show Loading
        # until it crosses LOADING_FRAME_THRESHOLD so the user doesn't
        # see the "stage 1 score 0" intermediate before the target
        # stage lands.
        playing = (1 <= stage <= 7) and (frame >= self.LOADING_FRAME_THRESHOLD)

        # Watcher signaled "reset in progress" via the flag file. We must
        # not clear it until we've seen the full reset cycle: frame
        # counter drops below threshold (proving stage_switch / restart
        # took effect — the previous high-frame reading was the OLD
        # stage), then climbs back up (proving the new game is healthy).
        # During this window, force load mode regardless of what memory
        # currently reads. Stale-flag guard: a full reset cycle takes
        # at most ~30s; if the flag is older than 60s the watcher died
        # mid-reset and we'd be stuck in load mode forever.
        if os.path.exists(self.RESETTING_FLAG_FILE):
            try:
                age = time.time() - os.path.getmtime(self.RESETTING_FLAG_FILE)
            except OSError:
                age = 0
            if age > 60:
                try:
                    os.unlink(self.RESETTING_FLAG_FILE)
                except OSError:
                    pass
                self._observed_post_reset_low_frame = False
                # fall through to normal play/load handling
            else:
                pass  # fall into the cycle-tracking block below
        if os.path.exists(self.RESETTING_FLAG_FILE):
            if not playing:
                # Reset has actually taken effect (frame dropped).
                self._observed_post_reset_low_frame = True
            if self._observed_post_reset_low_frame and playing:
                # Full cycle observed: drop then recover. Clear the flag
                # and let normal play-mode rendering resume.
                try:
                    os.unlink(self.RESETTING_FLAG_FILE)
                except OSError:
                    pass
                self._observed_post_reset_low_frame = False
            else:
                playing = False  # keep mask up through the transition
        else:
            # Flag absent — reset any stale tracking state.
            self._observed_post_reset_low_frame = False
        if playing:
            self._set_mode("play")
            canvas = self.play_canvas
            cw, ch = self.play_w, self.play_h
            lines = [f"score {score}"]
            font = self.font_play
            line_h = 12
            # play_win is sized to match the playfield x-range, so the
            # canvas center IS the playfield center.
            cx = cw // 2
            cy = ch // 2
        else:
            self._set_mode("load")
            canvas = self.load_canvas
            cw, ch = self.load_w, self.load_h
            dots = "." * (self._loading_dots + 1)
            lines = ["Loading" + dots]
            font = self.font_load
            line_h = 22
            # In debug mode load_win is a small box (140x48) centered on
            # the playfield. Canvas center IS the playfield center now
            # (we positioned the window there), so draw at cw/2, ch/2.
            cx = cw // 2
            cy = ch // 2
        canvas.delete("all")
        y0 = cy - (line_h * (len(lines) - 1)) // 2
        for i, line in enumerate(lines):
            y = y0 + i * line_h
            for dx in range(-self.outline_w, self.outline_w + 1):
                for dy in range(-self.outline_w, self.outline_w + 1):
                    if dx == 0 and dy == 0:
                        continue
                    canvas.create_text(
                        cx + dx, y + dy, text=line, fill="black",
                        font=font, anchor="center",
                    )
            canvas.create_text(
                cx, y, text=line, fill="white",
                font=font, anchor="center",
            )

    def _open(self):
        pid = find_pid(self.cfg["exe"])
        if pid is None:
            raise SystemExit(f"{self.cfg['exe']} not running")
        base = find_image_base(pid, self.cfg["exe"])
        self.pid = pid
        self.delta = (base - 0x00400000) if base else 0
        # /proc/<pid>/mem MUST be unbuffered — Python's default buffered 'rb'
        # caches 8KiB and subsequent reads return stale values, so the overlay
        # appears to freeze even though the address is updating in the game.
        self.fd = open(f"/proc/{pid}/mem", "rb", buffering=0)
        print(f"[+] {self.cfg['exe']} pid={pid} base={hex(base) if base else '?'}",
              file=sys.stderr)

    def _read(self, key):
        self.fd.seek(self.cfg[key] + self.delta)
        return struct.unpack("<I", self.fd.read(4))[0]

    def _tick(self):
        # Advance the Loading-dot animation on a 600ms period regardless
        # of read success. Keeps the animation smooth even if a brief
        # read failure happens (e.g. th12 mid-restart).
        now = time.monotonic()
        if now - self._loading_last_bump >= self.LOADING_DOT_PERIOD_MS / 1000:
            self._loading_dots = (self._loading_dots + 1) % 3
            self._loading_last_bump = now

        try:
            score = self._read("score") * 10
            stage = self._read("stage")
            frame = self._read("frame")
            self._draw(score, stage, frame)
        except OSError as e:
            # th12 disappeared mid-read (e.g. systemctl restart). Try to
            # re-attach. While re-attaching, show Loading (no data).
            try:
                self.fd.close()
                self._open()
            except SystemExit:
                pass
            self._draw(0, 0, 0)

        # Re-apply geometry every tick. Tk Toplevels in bare X don't
        # reliably hold position across show/hide cycles. Skip lift()
        # if hit flash is up: lifting would put us above the hit
        # flash and obscure it. The mask still does its wine-obscure
        # job at the same X-stack level (hit flash on top).
        if self._current_mode == "play":
            self.play_win.geometry(self.play_win._kiosk_geom)
            if not self._hit_was_active:
                self.play_win.lift()
        elif self._current_mode == "load":
            self.load_win.geometry(self.load_win._kiosk_geom)
            if not self._hit_was_active:
                self.load_win.lift()
        self.root.after(100, self._tick)

    def run(self):
        self.root.mainloop()


def main():
    argparse.ArgumentParser().parse_known_args()
    Overlay().run()


if __name__ == "__main__":
    main()

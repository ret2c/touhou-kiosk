#!/usr/bin/env python3
"""
Button daemon — listens for a single helper-button keypress across all
evdev keyboards and dispatches actions based on the kiosk's current
logical state.

Listens for a single keyboard key (default F12) across all evdev keyboards.
Parses clicks into patterns, looks up the action by current logical state,
dispatches the action.

State file: /tmp/touhou_state holds the current logical state, one of
{playing, hit_window, sleep}. On disk so multiple processes (button_daemon,
score_watcher, future helpers) can read/write it atomically.

Dispatch table:

  state         pattern         action
  ────────────  ──────────────  ──────────────────────────────────────────
  playing       short × 5       enter sleep mode
  playing       long (>=3s)     poweroff prompt (second long press confirms)
  playing       other           ignore (NO single-click hit sim in playing)
  hit_window   short × 1       touch /tmp/score_hit  (debug only; --debug-hit-sim)
  hit_window   long (>=3s)     poweroff prompt
  hit_window   other           ignore
  sleep         any short       wake
  sleep         long (>=3s)     poweroff prompt

hit_window detection:
  We query gate_bridge.py status — if MCU reports armed=True, treat current
  state as hit_window regardless of the state file. State file is the
  fallback when the gate isn't reachable.

Long-press / poweroff confirmation:
  First long press (>=3s) writes /tmp/touhou_poweroff_pending with timestamp.
  A second long press within 5s of the first => systemctl poweroff.
  This is the irreversible-action confirmation gate.

Sleep mode:
  - DPMS off on :0
  - Esc to th12 via xdotool to pause game (best-effort)
  - state -> sleep
  Wake reverses: DPMS on, Esc to th12 to unpause, state -> playing.

Run as root via systemd (needs /dev/input read access). DISPLAY=:0 is
inherited from the systemd unit so xset/xdotool reach the kiosk Xorg.
"""
import argparse
import errno
import fcntl
import os
import select
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone


# ---------- evdev plumbing -----------------------------------------------

# struct input_event on 64-bit Linux: 16-byte timeval + u16 type + u16 code + s32 value
INPUT_EVENT_FMT = "llHHi"
INPUT_EVENT_SIZE = struct.calcsize(INPUT_EVENT_FMT)

EV_KEY = 0x01

# ioctl numbers for EVIOCGBIT(EV_KEY, ...) and EVIOCGNAME
def _IOC(direction, type_, nr, size):
    return (direction << 30) | (size << 16) | (ord(type_) << 8) | nr

_IOC_READ = 2
def EVIOCGNAME(length):
    return _IOC(_IOC_READ, 'E', 0x06, length)
def EVIOCGBIT(ev, length):
    return _IOC(_IOC_READ, 'E', 0x20 + ev, length)

KEY_MAX = 0x2ff  # KEY_MAX in linux/input-event-codes.h (Linux 5.x)

# Mapping of supported stand-in keys (extend as needed)
KEY_NAMES = {
    "F12": 88,
    "F11": 87,
    "F10": 68,
    "F9":  67,
    "F8":  66,
    "BACKSLASH": 43,
    "GRAVE": 41,  # backtick / `
    "SPACE": 57,
    "ENTER": 28,
    "EQUAL": 13,  # = key on main row (shifted: +)
    "KPPLUS": 78,  # numeric keypad +
    "PLUS": 78,  # alias for KPPLUS — user-facing "+ key" maps to keypad +
    "V":   47,  # debug-only mapping for dev keyboards (manual hit-trigger during hit_window)
}


def discover_keyboards(target_keycode):
    """Return list of /dev/input/eventN paths whose key bitmap includes
    target_keycode. Probes EVIOCGBIT(EV_KEY) on each event device."""
    out = []
    try:
        nodes = sorted(os.listdir("/dev/input"))
    except OSError:
        return []
    bytes_needed = (KEY_MAX + 7) // 8
    for n in nodes:
        if not n.startswith("event"):
            continue
        path = "/dev/input/" + n
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            buf = fcntl.ioctl(fd, EVIOCGBIT(EV_KEY, bytes_needed),
                              b"\x00" * bytes_needed)
            byte_idx = target_keycode // 8
            bit_idx = target_keycode % 8
            if byte_idx < len(buf) and (buf[byte_idx] >> bit_idx) & 1:
                # Get device name for logs
                name_buf = fcntl.ioctl(fd, EVIOCGNAME(256), b"\x00" * 256)
                name = name_buf.split(b"\x00", 1)[0].decode("utf-8", "replace")
                out.append((path, name))
        except OSError:
            pass
        finally:
            os.close(fd)
    return out


# ---------- click parser -------------------------------------------------

class ClickParser:
    """Tracks key-down / key-up events from the stand-in button, emits
    one of: ('short_n', N) for N short clicks, or ('long', dur) for a
    long press."""

    def __init__(self, long_press_ms=3000, group_window_ms=400, on_event=None):
        self.long_press_ms = long_press_ms
        self.group_window_ms = group_window_ms
        self.on_event = on_event or (lambda *a: None)
        self.short_clicks = 0
        self.press_time = None
        self.last_release = 0.0
        self.lock = threading.Lock()
        self._timer = None

    def feed(self, value, timestamp):
        """value: 1=press, 0=release, 2=autorepeat (ignore)."""
        with self.lock:
            if value == 1:
                self.press_time = timestamp
            elif value == 0 and self.press_time is not None:
                held_ms = (timestamp - self.press_time) * 1000.0
                self.press_time = None
                if held_ms >= self.long_press_ms:
                    self._cancel_timer()
                    # If we had pending short clicks, dispatch them first.
                    if self.short_clicks:
                        self.on_event("short", self.short_clicks)
                        self.short_clicks = 0
                    self.on_event("long", held_ms / 1000.0)
                else:
                    self.short_clicks += 1
                    self.last_release = timestamp
                    self._schedule_dispatch()

    def _schedule_dispatch(self):
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(
            self.group_window_ms / 1000.0, self._dispatch_short
        )
        self._timer.daemon = True
        self._timer.start()

    def _dispatch_short(self):
        with self.lock:
            n = self.short_clicks
            self.short_clicks = 0
            self._timer = None
        if n:
            self.on_event("short", n)

    def _cancel_timer(self):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None


# ---------- state file ---------------------------------------------------

VALID_STATES = {"playing", "hit_window", "sleep"}

def state_path(args):
    return args.state_file

def read_state(args):
    try:
        with open(args.state_file, "r") as f:
            s = f.read().strip()
            if s in VALID_STATES:
                return s
    except FileNotFoundError:
        pass
    except OSError:
        pass
    return "playing"  # default

def write_state(args, new_state):
    if new_state not in VALID_STATES:
        raise ValueError("invalid state: %s" % new_state)
    tmp = args.state_file + ".tmp"
    with open(tmp, "w") as f:
        f.write(new_state + "\n")
    os.rename(tmp, args.state_file)


# ---------- gate query ---------------------------------------------------

def gate_armed(args):
    """Returns True if the gate MCU reports armed=True. False on errors
    (including MCU unreachable). State file is the fallback."""
    try:
        r = subprocess.run(
            [args.gate_bridge, "--socket", args.gate_socket, "status"],
            capture_output=True, text=True, timeout=1.5
        )
        out = r.stdout.strip()
        # Expect:  OK STATE armed=True remaining=... hb_age=...
        if "armed=True" in out:
            return True
        return False
    except Exception:
        return False


def current_logical_state(args):
    """Combine gate armed status with the state file."""
    if gate_armed(args):
        return "hit_window"
    return read_state(args)


# ---------- actions ------------------------------------------------------

def log(args, msg):
    line = "[%s] %s" % (
        datetime.now(timezone.utc).isoformat(timespec="milliseconds"), msg)
    print(line, flush=True)
    if args.log:
        try:
            with open(args.log, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass


def run_x(args, cmd, env_extra=None):
    env = os.environ.copy()
    env["DISPLAY"] = args.display
    if env_extra:
        env.update(env_extra)
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              env=env, timeout=3)
    except Exception as e:
        log(args, "X_CMD_FAIL %s err=%r" % (" ".join(cmd), e))
        return None


def action_score_hit(args):
    # Runtime production guardrail (belt-and-suspenders to the startup
    # check in main()). If the production sentinel was created AFTER the
    # daemon started, the startup guard didn't fire — but here at the
    # actual debug hit-sim moment we re-check and refuse loudly. Operators
    # can `sudo touch /etc/touhou-kiosk/production` to disable the
    # debug-hit path mid-run without restarting the service.
    if os.path.exists(args.production_sentinel):
        log(args, "BUTTON_REFUSED reason=production_sentinel_present "
            "sentinel=%s" % args.production_sentinel)
        return
    log(args, "ACTION score_hit -> touch %s" % args.gate_file)
    try:
        with open(args.gate_file, "w") as f:
            f.write(str(time.time()))
    except OSError as e:
        log(args, "BUTTON_FAIL %r" % e)


def action_enter_sleep(args):
    log(args, "ACTION enter_sleep")
    write_state(args, "sleep")
    # Pause the game (best effort)
    run_x(args, ["xdotool", "key", "Escape"])
    # DPMS off
    run_x(args, ["xset", "dpms", "force", "off"])


def action_wake(args):
    log(args, "ACTION wake")
    run_x(args, ["xset", "dpms", "force", "on"])
    # Unpause the game (best effort)
    run_x(args, ["xdotool", "key", "Escape"])
    write_state(args, "playing")


def action_poweroff_prompt(args, hold_seconds):
    """First long press writes /tmp/touhou_poweroff_pending with timestamp.
    Second long press within 5s confirms and runs systemctl poweroff."""
    pending = "/tmp/touhou_poweroff_pending"
    now = time.time()
    if os.path.exists(pending):
        try:
            with open(pending) as f:
                t = float(f.read().strip())
            if now - t <= 5:
                log(args, "ACTION poweroff confirmed (held=%.1fs)" % hold_seconds)
                try:
                    os.unlink(pending)
                except OSError:
                    pass
                subprocess.run(["sudo", "systemctl", "poweroff"], check=False)
                return
        except (OSError, ValueError):
            pass
    log(args, "ACTION poweroff_prompt (first long press; hold again within 5s to confirm)")
    with open(pending, "w") as f:
        f.write(str(now))


# ---------- dispatch ------------------------------------------------------

def dispatch(args, kind, payload):
    state = current_logical_state(args)
    log(args, "DISPATCH state=%s kind=%s payload=%s" % (state, kind, payload))

    if kind == "long":
        action_poweroff_prompt(args, payload)
        return

    # kind == "short", payload = click count
    n = payload
    if state == "sleep":
        action_wake(args)
        return

    # --debug-anytime-hit: every single short press touches the hit file,
    # regardless of state. For testing the gate_bridge / hit flash plumbing
    # without having to actually cross the score threshold first.
    if args.debug_anytime_hit and n == 1:
        log(args, "DEBUG_ANYTIME_HIT short=1 state=%s" % state)
        action_score_hit(args)
        return

    if state == "hit_window":
        if n == 1 and args.debug_hit_sim:
            action_score_hit(args)
        else:
            log(args, "IGNORE state=hit_window short=%d (debug_hit_sim=%s)"
                % (n, args.debug_hit_sim))
        return

    # state == "playing"
    if n == 5:
        action_enter_sleep(args)
    else:
        log(args, "IGNORE state=playing short=%d" % n)


# ---------- main loop -----------------------------------------------------

def listen_loop(args, key_code):
    parser = ClickParser(
        long_press_ms=int(args.long_press_seconds * 1000),
        group_window_ms=args.group_window_ms,
        on_event=lambda kind, payload: dispatch(args, kind, payload),
    )

    # path -> fd map. We rescan periodically and add new keyboards as they
    # appear (e.g. user plugs in a USB keyboard, or test injector creates a
    # uinput device after this daemon started).
    open_fds = {}

    def rescan():
        found = discover_keyboards(key_code)
        seen = set()
        for path, name in found:
            seen.add(path)
            if path in open_fds:
                continue
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                open_fds[path] = fd
                log(args, "ATTACH device=%s name=%r fd=%d" % (path, name, fd))
            except OSError as e:
                log(args, "OPEN_FAIL %s err=%r" % (path, e))
        # Drop devices that vanished
        for path in list(open_fds.keys()):
            if path not in seen:
                try:
                    os.close(open_fds[path])
                except OSError:
                    pass
                log(args, "DETACH device=%s" % path)
                del open_fds[path]

    rescan()
    log(args, "READY key_code=%d long_press_s=%.1f group_window_ms=%d "
        "debug_hit_sim=%s open_devices=%d"
        % (key_code, args.long_press_seconds, args.group_window_ms,
           args.debug_hit_sim, len(open_fds)))

    last_rescan = time.time()
    while True:
        # Don't crash if no devices yet — keep rescanning.
        fds = list(open_fds.values())
        if fds:
            try:
                r, _, _ = select.select(fds, [], [], 1.0)
            except OSError:
                r = []
        else:
            time.sleep(1.0)
            r = []

        # Periodic rescan every ~1.5s to pick up hotplug / uinput devices
        if time.time() - last_rescan > 1.5:
            rescan()
            last_rescan = time.time()

        for fd in r:
            try:
                buf = os.read(fd, INPUT_EVENT_SIZE * 64)
            except OSError as e:
                if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    continue
                # Device went away (ENODEV/EBADF). Close it now and let the
                # next rescan reconcile open_fds. Don't spam every iteration.
                if e.errno in (errno.ENODEV, errno.EBADF, errno.ENXIO):
                    drop_path = None
                    for p, dfd in list(open_fds.items()):
                        if dfd == fd:
                            drop_path = p
                            break
                    if drop_path:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                        del open_fds[drop_path]
                        log(args, "DETACH_ENODEV device=%s" % drop_path)
                    continue
                log(args, "READ_FAIL fd=%d err=%r" % (fd, e))
                continue
            for i in range(0, len(buf), INPUT_EVENT_SIZE):
                rec = buf[i:i + INPUT_EVENT_SIZE]
                if len(rec) < INPUT_EVENT_SIZE:
                    break
                sec, usec, etype, ecode, evalue = struct.unpack(
                    INPUT_EVENT_FMT, rec)
                if etype != EV_KEY:
                    continue
                # Touch the kbd-event sentinel for ANY key event (any
                # key, any direction). The mtime of this file is a cheap
                # "any keyboard activity happened" sentinel that other
                # daemons can poll without opening /dev/input directly.
                if evalue == 1 and getattr(args, "kbd_event_file", None):
                    # Explicit mtime bump. Opening for append + close
                    # without writing does NOT update mtime in Python
                    # (mtime tracks content changes, not open-for-
                    # write). os.utime(path, None) sets atime+mtime
                    # to "now" reliably. If the file doesn't exist
                    # yet, create it first (one-time cost).
                    try:
                        os.utime(args.kbd_event_file, None)
                    except FileNotFoundError:
                        try:
                            with open(args.kbd_event_file, "wb"):
                                pass
                        except OSError:
                            pass
                    except OSError:
                        pass
                if ecode != key_code:
                    continue
                ts = sec + usec * 1e-6
                parser.feed(evalue, ts)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--key", default="F12",
                   choices=sorted(KEY_NAMES.keys()),
                   help="stand-in key for the GPIO button")
    p.add_argument("--state-file", default="/tmp/touhou_state")
    p.add_argument("--gate-file", default="/tmp/score_hit")
    p.add_argument("--kbd-event-file", default="/tmp/touhou_kbd_event",
                   help="touched (mtime updated) on ANY key down event "
                        "across all watched keyboards. A cheap sentinel "
                        "for 'any keyboard activity happened' that other "
                        "daemons can poll without opening /dev/input.")
    p.add_argument("--gate-socket", default="/tmp/touhou_gate.sock")
    p.add_argument("--gate-bridge",
                   default="/home/ubuntu/touhou-kiosk/gate_bridge.py")
    p.add_argument("--display", default=":0")
    p.add_argument("--long-press-seconds", type=float, default=3.0,
                   help="hold duration that classifies as long press")
    p.add_argument("--group-window-ms", type=int, default=400,
                   help="dispatch short-click count after this much idle")
    p.add_argument("--debug-hit-sim", action="store_true",
                   help="single click in hit_window touches /tmp/score_hit. "
                        "ENABLE only for software-only testing — refused at "
                        "startup if /etc/touhou-kiosk/production exists.")
    p.add_argument("--debug-anytime-hit", action="store_true",
                   help="any single short click touches /tmp/score_hit, "
                        "regardless of state (playing, sleep, hit_window). "
                        "For testing the flash overlay's hit-display without "
                        "having to cross the score threshold first. Same "
                        "production-sentinel block as --debug-hit-sim.")
    p.add_argument("--production-sentinel",
                   default="/etc/touhou-kiosk/production",
                   help="if this file exists, --debug-hit-sim is refused at "
                        "startup. `sudo touch` this file to permanently "
                        "neutralize the debug hit-sim path on a deployed unit.")
    p.add_argument("--log", default="/tmp/button_daemon.log")
    args = p.parse_args()

    # Production guardrail: a deployed unit cannot accidentally ship with
    # --debug-hit-sim if the operator has marked the device as production.
    # This is a hard refusal — the daemon won't start. The intent is to
    # make the failure very visible at bring-up time, not silently
    # downgrade the flag, so a misconfigured systemd unit gets caught
    # immediately rather than discovered after a real input event.
    if args.debug_hit_sim and os.path.exists(args.production_sentinel):
        print(
            "[FATAL] --debug-hit-sim is set but production sentinel exists "
            "at %s. Refusing to start. Either remove --debug-hit-sim from "
            "the systemd unit (production) or remove the sentinel file "
            "(returning to debug)." % args.production_sentinel,
            file=sys.stderr,
        )
        sys.exit(2)
    if args.debug_anytime_hit and os.path.exists(args.production_sentinel):
        print(
            "[FATAL] --debug-anytime-hit is set but production sentinel "
            "exists at %s. Refusing to start." % args.production_sentinel,
            file=sys.stderr,
        )
        sys.exit(2)

    key_code = KEY_NAMES[args.key]
    listen_loop(args, key_code)


if __name__ == "__main__":
    main()

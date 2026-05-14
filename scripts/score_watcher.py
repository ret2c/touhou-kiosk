#!/usr/bin/env python3
"""Score-gate watcher for Touhou TH12.

Polls the running game's score address; when threshold crossed:
  1. Fires the hit unlock hook (configurable shell command)
  2. Optionally locks user keyboard via xinput
  3. Sends the in-game "restart current stage" key sequence:
        Esc → Up → Z → Up → Z
     which opens the pause menu, navigates to the second sub-option, confirms
     the "return / retry" path, and lands the player back at stage 1 start
     with score reset to 0 by the game itself.
  4. Releases the keyboard lock.

Run as root (needs /proc/<pid>/mem write + xinput control).

Usage:
    sudo ./score_watcher.py th12 --threshold 200000 --hook 'echo GATE_EVENT'
"""

import argparse
import atexit
import os
import random
import re
import signal
import struct
import subprocess
import sys
import time
import traceback


# ---- Death diagnostics --------------------------------------------------
# In a prior session the watcher disappeared between log entries with no
# traceback in /tmp/watcher.log, no journal record, no OOM, no auth log
# evidence. Cause was never identified. Adding signal handlers + an
# atexit hook so any future death leaves a fingerprint in the log.

_DEATH_LOG_PATH = "/tmp/watcher.log"


def _death_print(msg):
    """Write a single line directly to the watcher log + stderr, flushing
    aggressively. Used from signal handlers and atexit; must not raise."""
    try:
        line = "[!! DEATH] %s\n" % msg
        try:
            sys.stderr.write(line)
            sys.stderr.flush()
        except Exception:
            pass
        try:
            with open(_DEATH_LOG_PATH, "a") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            pass
    except Exception:
        pass


def _install_death_diagnostics():
    """Install signal handlers, atexit, and excepthook so the watcher
    can't die quietly. Idempotent."""
    def _sig_handler(signum, frame):
        try:
            name = signal.Signals(signum).name
        except Exception:
            name = "signal_%d" % signum
        _death_print("received %s ppid=%d" % (name, os.getppid()))
        # Re-raise the default handler so the process actually dies.
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
        try:
            signal.signal(sig, _sig_handler)
        except Exception:
            pass

    def _excepthook(exc_type, exc_value, tb):
        text = "".join(traceback.format_exception(exc_type, exc_value, tb))
        _death_print("uncaught_exception:\n%s" % text)
        sys.__excepthook__(exc_type, exc_value, tb)

    sys.excepthook = _excepthook

    def _on_exit():
        _death_print("exit ppid=%d" % os.getppid())

    atexit.register(_on_exit)

TH12 = {
    "exe": "th12.exe",
    "score": 0x004B0C44,
    "score_mult": 10,
    "stage": 0x004B0CB0,
    "frame": 0x004B0CBC,
    "lives": 0x004B0CA0,
    # stage_struct_ptr — NULL when player is on title / game-over /
    # continue menu (no active stage struct allocated). Non-NULL
    # during real gameplay. Reliable title-screen detect signal.
    "sptr": 0x004B44E8,
}


def find_pid(exe):
    # During native_restart there's a ~100ms window where the old th12
    # and new th12 coexist. We want the new one. Skip zombies (State=Z)
    # and processes with empty maps (kernel hasn't finished setup or
    # they're tearing down), and prefer the highest pid among the rest
    # — on Linux PIDs are allocated monotonically until rollover, so
    # the youngest process has the largest pid (modulo a 4M rollover
    # window we'd never hit in a kiosk uptime).
    candidates = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/comm") as f:
                if f.read().strip() != exe:
                    continue
            with open(f"/proc/{pid}/status") as f:
                state_line = next(
                    (ln for ln in f if ln.startswith("State:")), "")
            if "\tZ" in state_line or " Z" in state_line:
                continue
            with open(f"/proc/{pid}/maps") as f:
                if not f.read(64):
                    continue
            candidates.append(int(pid))
        except (FileNotFoundError, PermissionError, StopIteration):
            pass
    if not candidates:
        return None
    return max(candidates)


def _xenv(display):
    return {**os.environ, "DISPLAY": display}


def _prime_focus(display):
    """Find the wine game window and call windowactivate. The activate call
    errors with "no _NET_ACTIVE_WINDOW" on bare Xorg but the side effect
    primes X focus state so XTest events from `xdotool key` reach Wine."""
    env = _xenv(display)
    out = subprocess.run(
        ["xdotool", "search", "--name", "Undefined"],
        capture_output=True, text=True, env=env,
    ).stdout.strip().split("\n")
    win = out[0] if out and out[0] else None
    if win:
        subprocess.run(
            ["xdotool", "windowactivate", "--sync", win],
            stderr=subprocess.DEVNULL, env=env,
        )
    return win


def restart_stage(display=":0"):
    """Restart the kiosk from scratch via systemd. ~25-30s downtime.
    Now the PRIMARY reset path after the primary recovery path  — only a
    full service restart clears TH12's engine state (bloom RT etc).

    Paints the X root window solid black BEFORE issuing the restart.
    Without this, when score_overlay dies (it's inside the service
    cgroup), the X root's default gray pattern is exposed for the
    few seconds of restart gap. Black root means the gap reads as
    "black mask still up" instead of "kiosk crashed."
    """
    env = _xenv(display)
    subprocess.run(
        ["xsetroot", "-solid", "black"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    print("[+] restarting touhou-kiosk.service for stage reset", flush=True)
    subprocess.run(
        ["sudo", "systemctl", "restart", "touhou-kiosk.service"],
        check=False,
    )
    time.sleep(60)


def _read_frame(pid):
    """Read TH12's frame counter at fixed offset. Returns int or None."""
    import struct
    try:
        with open(f"/proc/{pid}/mem", "rb", buffering=0) as f:
            f.seek(0x004B0CBC)
            return struct.unpack("<I", f.read(4))[0]
    except (OSError, struct.error):
        return None


def _frame_advanced(pid, prior_frame, settle_s=2.0):
    """Returns True if frame counter has advanced past prior_frame +60
    OR reset way below it (= stage restarted). Sleeps settle_s first."""
    time.sleep(settle_s)
    f1 = _read_frame(pid)
    if f1 is None:
        return False
    return (f1 > prior_frame + 60) or (f1 < prior_frame - 1000)


# Linux kernel keycodes for the keys native_continue uses.
# (See linux/input-event-codes.h)
_KEY_Z = 44
_KEY_ESCAPE = 1
_KEY_UP = 103
_KEY_DOWN = 108

_UINPUT_PATH = "/dev/uinput"
_UI_SET_EVBIT = 0x40045564
_UI_SET_KEYBIT = 0x40045565
_UI_DEV_CREATE = 0x5501
_UI_DEV_DESTROY = 0x5502
_EV_SYN = 0
_EV_KEY = 1
_SYN_REPORT = 0
_INPUT_EVENT_FMT = "llHHi"


def _make_uinput_dev_struct(name="TouhouNcInject"):
    name_b = name.encode("utf-8")[:79].ljust(80, b"\x00")
    input_id = struct.pack("HHHH", 0x03, 0xDEAD, 0xBEEF, 0x0002)
    ff_effects_max = struct.pack("I", 0)
    abs_arrays = b"\x00" * (4 * 64 * 4)
    return name_b + input_id + ff_effects_max + abs_arrays


def _uinput_send_key(fd, keycode, value):
    """Write one input_event to uinput."""
    t = time.time()
    sec = int(t)
    usec = int((t - sec) * 1e6)
    os.write(fd, struct.pack(_INPUT_EVENT_FMT, sec, usec, _EV_KEY, keycode, value))


def _uinput_syn(fd):
    t = time.time()
    sec = int(t)
    usec = int((t - sec) * 1e6)
    os.write(fd, struct.pack(_INPUT_EVENT_FMT, sec, usec, _EV_SYN, _SYN_REPORT, 0))


def uinput_inject_sequence(sequence):
    """Inject a sequence of key tap events via /dev/uinput.

    `sequence` is a list of (keycode, hold_seconds) tuples. Each tap
    sends keydown, holds for hold_seconds, sends keyup, then a small
    inter-tap delay. Wine's X11 driver picks up these as REAL physical
    keypresses (kernel-generated EV_KEY events flow through evdev →
    libinput → X11 driver), bypassing the synthetic-XSendEvent
    rejection that xdotool's `key --window` hits.

    Uses the same kernel input plumbing the button daemon uses for
    the F12 helper button.

    Caveats:
    - Needs root (uinput device creation).
    - Wine needs ~2s to register the new keyboard device after
      UI_DEV_CREATE (the kernel-side hotplug + X11/evdev rescan).
    - The new keyboard is global to the X session; ANY focused window
      receives the keypresses, not just TH12. In our kiosk this is
      fine because TH12 is the only X client other than the score
      overlay.
    """
    import fcntl
    fd = os.open(_UINPUT_PATH, os.O_WRONLY | os.O_NONBLOCK)
    created = False
    try:
        fcntl.ioctl(fd, _UI_SET_EVBIT, _EV_KEY)
        # Enable each keycode we'll use
        for kc in set(k for k, _ in sequence):
            fcntl.ioctl(fd, _UI_SET_KEYBIT, kc)
        os.write(fd, _make_uinput_dev_struct())
        # UI_DEV_CREATE returns 0 on success; raises OSError on
        # failure (which the outer caller's OSError-catch handles).
        # If it succeeds but the device is in a half-state for some
        # reason, the subsequent write()s here would also raise — so
        # explicit return-check isn't strictly necessary, but the
        # `created` flag below ensures UI_DEV_DESTROY isn't called
        # on a device that was never created (would just be ENODEV
        # noise in the logs).
        fcntl.ioctl(fd, _UI_DEV_CREATE)
        created = True
        # Wait for wine/X11 to register the device. Without this
        # delay, the first few events get dropped because no client
        # is reading them yet.
        time.sleep(1.5)
        for keycode, hold_s in sequence:
            _uinput_send_key(fd, keycode, 1)  # keydown
            _uinput_syn(fd)
            time.sleep(hold_s)
            _uinput_send_key(fd, keycode, 0)  # keyup
            _uinput_syn(fd)
            time.sleep(0.15)
        # Let the last keypress flow through before destroying the
        # device (otherwise it gets dropped by the cleanup race).
        time.sleep(0.5)
    finally:
        if created:
            try:
                fcntl.ioctl(fd, _UI_DEV_DESTROY)
            except OSError:
                pass
        os.close(fd)


_NC_DIAG_COUNTER = [0]


def _diag_shot(env, label):
    """Best-effort screenshot for diagnostics. Idempotent path under
    /tmp/nc_diag/. Counter ensures unique file per call site even
    across cycles."""
    _NC_DIAG_COUNTER[0] += 1
    n = _NC_DIAG_COUNTER[0]
    os.makedirs("/tmp/nc_diag", exist_ok=True)
    path = f"/tmp/nc_diag/{n:04d}_{label}.png"
    try:
        subprocess.run(["scrot", path], capture_output=True, env=env, timeout=3)
    except Exception:
        pass
    return path


def native_continue(display=":0", pid=None, diag=False):
    """Use TH12's own native menu mechanisms to dismiss any visible
    menu and restart the current stage. Two-stage approach:

      Stage 1 — send Z. Handles game-over screen (default-highlighted
        "Continue / restart current stage" option).

      Stage 2 — Esc + Up + Z + Up + Z. Opens pause menu, navigates
        Up to Retry, Z opens confirmation dialog (default highlight =
        No), Up to Yes, Z confirms. Stage restarts.

    Returns True if frame counter advances or resets within a few
    seconds. Returns False so caller can escalate to systemctl restart.

    Empirically  `xdotool key --window WID` does
    NOT reach wine when called from the watcher's sudo'd context — wine
    drops the synthetic XSendEvent. We use /dev/uinput instead: kernel
    input events flow through evdev → libinput → X11 driver and
    arrive at wine as REAL physical keys, the same path the button
    daemon uses for the F12 helper button.

    Both stages are now sent in a single uinput device session: 1.5s
    discovery wait + key sequence + cleanup. Total ~2-3s per call.

    diag=True dumps screenshots to /tmp/nc_diag/ at each substep.
    """
    env = {**os.environ, "DISPLAY": display}

    if diag:
        _diag_shot(env, "00_initial")

    # ---- Stage 1: Z alone (game-over screen)
    f0 = _read_frame(pid) if pid else None
    print("[+] native_continue stage 1: uinput Z", flush=True)
    try:
        uinput_inject_sequence([(_KEY_Z, 0.10)])
    except OSError as e:
        print(f"[!] native_continue: uinput Z failed: {e}", flush=True)
        return False
    if diag:
        time.sleep(0.3)
        _diag_shot(env, "01_after_stage1_Z")
    if pid is None:
        return True
    if _frame_advanced(pid, f0, settle_s=1.5):
        f1 = _read_frame(pid)
        print(f"[+] native_continue: stage 1 succeeded; frame {f0} -> {f1}",
              flush=True)
        if diag:
            _diag_shot(env, "02_stage1_success")
        return True
    if diag:
        _diag_shot(env, "02_stage1_failed_pre_stage2")

    # ---- Stage 2: Esc + Up + Z + Up + Z (Retry from pause menu
    # with confirm-dialog Yes)
    f0 = _read_frame(pid)
    print(f"[+] native_continue stage 2: uinput Esc+Up+Z+Up+Z; frame={f0}",
          flush=True)
    try:
        uinput_inject_sequence([
            (_KEY_ESCAPE, 0.10),
            (_KEY_UP, 0.10),
            (_KEY_Z, 0.10),
            (_KEY_UP, 0.10),
            (_KEY_Z, 0.10),
        ])
    except OSError as e:
        print(f"[!] native_continue: uinput Esc+Up+Z+Up+Z failed: {e}",
              flush=True)
        return False
    if diag:
        time.sleep(0.3)
        _diag_shot(env, "05_after_stage2_Z")
    if _frame_advanced(pid, f0, settle_s=2.0):
        f1 = _read_frame(pid)
        print(f"[+] native_continue: stage 2 succeeded; frame {f0} -> {f1}",
              flush=True)
        if diag:
            _diag_shot(env, "06_stage2_success")
        return True

    f1 = _read_frame(pid)
    print(f"[!] native_continue: both stages failed; frame {f0} -> {f1}",
          flush=True)
    if diag:
        _diag_shot(env, "06_both_failed")
    return False


def th12_restart_via_wine(display=":0",
                          wine_prefix="/home/ubuntu/.wine_touhou",
                          wine_bin="/opt/touhou-hangover/usr/bin/wine",
                          helper_exe="/home/ubuntu/touhou-kiosk/th12_restart_inject.exe"):
    """Invoke TH12's internal stage-restart from the game thread via a
    Windows helper DLL (th12_restart_inject.exe + th12_restart.dll).

    The loader (.exe) does CreateRemoteThread + LoadLibraryA to inject
    the .dll into th12. The .dll's worker thread:
      1. FindWindowA + GetWindowThreadProcessId in-process to learn
         th12's main TID (in-process FindWindow works under Wine even
         though cross-process EnumWindows does not).
      2. SetWindowsHookExA(WH_GETMESSAGE, ..., main_tid) to install a
         thread-targeted hook on the main thread.
      3. PostMessage(WM_NULL) to wake the message pump.
      4. Hook fires on the main thread, runs the 21-byte payload
         (call 0x422770; push 0; call 0x422700) — the same code path
         TH12's pause-menu Retry uses. Because the calls run on the
         main thread, the engine's next-frame task dispatcher sees
         the registered 0x00422280 callback and fires it cleanly.

    Returns True only after helper success (exit code 0) and
    frame-counter verification. Caller falls back to systemctl restart
    on False.

    Helper exit codes (per th12_restart_inject.c):
      0 = success
      1 = th12.exe not found
      2 = OpenProcess failed
      3 = VirtualAllocEx failed
      4 = WriteProcessMemory failed
      5 = CreateRemoteThread / LoadLibraryA failed
      6 = remote thread didn't return in time
      7 = preconditions failed (caller shouldn't have invoked us)
    """
    if not os.path.exists(helper_exe):
        print(f"[!] th12_restart_via_wine: helper missing at {helper_exe}",
              flush=True)
        return False
    # The watcher runs as root (sudo) but the wine prefix is owned by
    # ubuntu. Wine >= 10 refuses prefixes not owned by the running
    # user, so we sudo back to ubuntu to invoke the helper. The
    # ubuntu user can ptrace th12 because it owns the th12 process
    # too — th12 was spawned by the kiosk launcher as ubuntu.
    print(f"[+] th12_restart_via_wine: spawning {helper_exe} as ubuntu",
          flush=True)
    try:
        r = subprocess.run(
            ["sudo", "-u", "ubuntu",
             "env",
             f"DISPLAY={display}",
             f"WINEPREFIX={wine_prefix}",
             "HOME=/home/ubuntu",
             "WINEDEBUG=-all",
             "PATH=/opt/touhou-hangover/usr/bin:/usr/bin:/bin",
             wine_bin, helper_exe],
            capture_output=True, text=True, timeout=20,
        )
    except subprocess.TimeoutExpired as e:
        # Wine helper wedged. Don't let TimeoutExpired propagate — that
        # would kill the watcher and bypass the reset ladder. Return
        # False so the caller falls back to the next rung.
        print(f"[!] th12_restart_via_wine: TIMEOUT after {e.timeout}s; "
              f"helper wedged, falling back", flush=True)
        return False
    if r.returncode == 0:
        for line in (r.stderr or "").strip().splitlines():
            print(f"[+]   helper: {line}", flush=True)
        # Post-helper verify: helper rc=0 just means the loader
        # subprocess exited cleanly, NOT that the in-process hook
        # actually fired the restart payload. Sample the frame
        # counter — if the restart fired, it dropped to ~0 and
        # is now ticking up at 60fps. If the helper loaded the DLL
        # but the hook never fired (main thread in a weird state
        # post-SIGCONT, dispatch never reached GetMessage, etc),
        # the frame counter would be wherever it was before the
        # call (possibly large, possibly stuck). Require frame to
        # be in a "just restarted" range.
        try:
            pid_now = find_pid("th12.exe")
            if pid_now is None:
                print(f"[!] th12_restart_via_wine: verify failed — th12 gone",
                      flush=True)
                return False
            with open(f"/proc/{pid_now}/mem", "rb", buffering=0) as f:
                f.seek(0x004B0CBC)
                fr = struct.unpack("<I", f.read(4))[0]
        except OSError as e:
            print(f"[!] th12_restart_via_wine: verify read failed: {e}",
                  flush=True)
            return False
        # Frame should be in early stage 1 range (the helper sleeps
        # 250 ms before returning, so frame has ticked ~15 frames
        # since reset). 600 = generous upper bound that catches any
        # delay but rejects "frame is still at thousands like before".
        if fr < 600:
            print(f"[+] th12_restart_via_wine: success (frame={fr})",
                  flush=True)
            return True
        print(f"[!] th12_restart_via_wine: helper rc=0 BUT frame={fr} — "
              f"hook didn't actually fire (DLL loaded but no payload run)",
              flush=True)
        return False
    print(f"[!] th12_restart_via_wine: helper rc={r.returncode}", flush=True)
    for line in (r.stdout or "").strip().splitlines():
        print(f"[!]   stdout: {line}", flush=True)
    for line in (r.stderr or "").strip().splitlines():
        print(f"[!]   stderr: {line}", flush=True)
    return False


def th12_stageswitch_via_wine(target_stage,
                              display=":0",
                              wine_prefix="/home/ubuntu/.wine_touhou",
                              wine_bin="/opt/touhou-hangover/usr/bin/wine",
                              helper_exe="/home/ubuntu/games/th12/th12_stageswitch_inject.exe",
                              dll_name="Z:\\home\\ubuntu\\games\\th12\\th12_stageswitch_v4.dll"):
    """Stage-switch via the in-process DLL. Same teardown+init-task
    pattern as th12_restart_via_wine, but writes STAGE_NUM, STAGE_BACKUP,
    and STAGE_TABLE_PTR right before the call sequence so the engine
    boots into target_stage (1..6) instead of restarting the current
    stage. Supports TH12 v1.00b stages 1..6 on the OPi target.

    The helper exe finds th12's CWD via Module32First, writes a single
    ASCII digit '1'..'6' into <cwd>/stageswitch_target, then injects
    th12_stageswitch.dll via CreateRemoteThread + LoadLibraryA. The DLL
    reads the file on attach and uses the digit as the target.

    target_stage: int 1..6
    Returns True on success, False otherwise. Caller falls back to
    th12_restart_via_wine, then to systemctl restart, on False.
    """
    if not (1 <= target_stage <= 6):
        print(f"[!] th12_stageswitch_via_wine: target {target_stage} not in 1..6",
              flush=True)
        return False
    if not os.path.exists(helper_exe):
        print(f"[!] th12_stageswitch_via_wine: helper missing at {helper_exe}",
              flush=True)
        return False
    print(f"[+] th12_stageswitch_via_wine: target={target_stage}",
          flush=True)
    try:
        r = subprocess.run(
            ["sudo", "-u", "ubuntu",
             "env",
             f"DISPLAY={display}",
             f"WINEPREFIX={wine_prefix}",
             "HOME=/home/ubuntu",
             "WINEDEBUG=-all",
             "PATH=/opt/touhou-hangover/usr/bin:/usr/bin:/bin",
             wine_bin, helper_exe, "--stage", str(target_stage),
             "--dll", dll_name],
            capture_output=True, text=True, timeout=20,
        )
    except subprocess.TimeoutExpired as e:
        print(f"[!] th12_stageswitch_via_wine: TIMEOUT after {e.timeout}s; "
              f"helper wedged, falling back", flush=True)
        return False
    if r.returncode != 0:
        print(f"[!] th12_stageswitch_via_wine: helper rc={r.returncode}",
              flush=True)
        for line in (r.stderr or "").strip().splitlines():
            print(f"[!]   stderr: {line}", flush=True)
        return False
    for line in (r.stderr or "").strip().splitlines():
        print(f"[+]   helper: {line}", flush=True)
    # Post-helper verify: STAGE_NUM should now == target_stage AND frame
    # counter should be in "just restarted" range (the helper sleeps 2.5s
    # so frame ticks ~150).
    try:
        pid_now = find_pid("th12.exe")
        if pid_now is None:
            print(f"[!] th12_stageswitch_via_wine: verify failed — th12 gone",
                  flush=True)
            return False
        with open(f"/proc/{pid_now}/mem", "rb", buffering=0) as f:
            f.seek(0x004B0CB0)
            sn = struct.unpack("<I", f.read(4))[0]
            f.seek(0x004B0CBC)
            fr = struct.unpack("<I", f.read(4))[0]
    except OSError as e:
        print(f"[!] th12_stageswitch_via_wine: verify read failed: {e}",
              flush=True)
        return False
    if sn == target_stage and 0 < fr < 600:
        print(f"[+] th12_stageswitch_via_wine: success (stage={sn} frame={fr})",
              flush=True)
        return True
    print(f"[!] th12_stageswitch_via_wine: verify mismatch "
          f"(stage={sn} expected {target_stage}, frame={fr})", flush=True)
    return False


def lock_keyboard(device_id, display=":0"):
    subprocess.run(
        ["xinput", "disable", str(device_id)],
        env=_xenv(display), stderr=subprocess.DEVNULL,
    )


def unlock_keyboard(device_id, display=":0"):
    subprocess.run(
        ["xinput", "enable", str(device_id)],
        env=_xenv(display), stderr=subprocess.DEVNULL,
    )


def find_user_keyboard(display=":0"):
    """Heuristic: pick the first slave keyboard whose name doesn't look like
    a virtual/builtin device. Returns numeric xinput id or None."""
    env = _xenv(display)
    out = subprocess.run(
        ["xinput", "list", "--short"],
        capture_output=True, text=True, env=env,
    ).stdout
    for line in out.splitlines():
        if "slave  keyboard" not in line:
            continue
        if any(s in line for s in ("XTEST", "Virtual", "Power", "rk805",
                                    "adc-keys", "headset-keys")):
            continue
        m = re.search(r"id=(\d+)", line)
        if m:
            return int(m.group(1))
    return None


def show_hit_flash(display=":0", duration=2.5,
                    gate_file="/tmp/score_hit"):
    """Big HIT box centered on visible playfield with:
      - blinking "HIT" header
      - countdown timer (Ns remaining)
      - button-status line: red "button not pressed" or green
        "button is being pressed", polling gate_file.

    Returns the Popen handle so caller can kill early on hit.

    xrandr playfield-crop maps fb (32,16)-(416,464) to full panel
    480x800. Panel center (240, 400) ↔ fb (224, 240).
    """
    flash_py = """
import tkinter as tk, time, math, os, atexit, signal
W, H = 170, 78
CX_FB, CY_FB = 224, 240
ox = CX_FB - W // 2
oy = CY_FB - H // 2
GATE_FILE = %r
# score_overlay.py polls for /tmp/hit_active and hides its play/load
# windows while we exist, so the flash is the only thing centered on
# the playfield.
ACTIVE_FILE = '/tmp/hit_active'
try: open(ACTIVE_FILE, 'w').close()
except OSError: pass
def _cleanup(*_):
    try: os.unlink(ACTIVE_FILE)
    except OSError: pass
atexit.register(_cleanup)
signal.signal(signal.SIGTERM, lambda *_: (_cleanup(), os._exit(0)))
r = tk.Tk(); r.overrideredirect(True); r.attributes('-topmost', True)
r.configure(bg='black')
r.geometry(f'{W}x{H}+{ox}+{oy}')
top = tk.Label(r, text='HIT', fg='white', bg='black',
               font=('DejaVu Sans', 16, 'bold'))
top.place(relx=0.5, rely=0.22, anchor='center')
mid = tk.Label(r, text='', fg='white', bg='black',
               font=('DejaVu Sans Mono', 11, 'bold'))
mid.place(relx=0.5, rely=0.50, anchor='center')
bot = tk.Label(r, text='', fg='red', bg='black',
               font=('DejaVu Sans', 8, 'bold'))
bot.place(relx=0.5, rely=0.80, anchor='center')
end = time.monotonic() + %f
def tick():
    now = time.monotonic()
    on = int(now * 4) %% 2
    top.configure(text='HIT' if on else '')
    remaining = max(0, math.ceil(end - now))
    mid.configure(text=f'{remaining}s')
    if os.path.exists(GATE_FILE):
        bot.configure(text='button is being pressed', fg='#00d000')
    else:
        bot.configure(text='button not pressed', fg='#ff3030')
    if now < end:
        r.after(120, tick)
    else:
        r.destroy()
r.after(50, tick); r.mainloop()
""" % (gate_file, duration)
    return subprocess.Popen(
        ["python3", "-c", flash_py],
        env=_xenv(display),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


OVERLAY_PATH = "/home/ubuntu/touhou-kiosk/score_overlay.py"


def _crtc_renudge(display=":0"):
    """Force a CRTC re-scan to drop any stale DMA-BUF reference (e.g.
    a wine DMA-BUF that the CRTC was page-flipping just before wine
    died). Toggles the xrandr transform to a near-identical value and
    back. modesetting requires the new transform to differ from the
    current one (it short-circuits identical sets), so we add a
    1-bit nudge then restore."""
    env = _xenv(display)
    t0 = time.monotonic()
    # --fb 640x480 pins the screen size so xrandr doesn't try to
    # auto-recompute it from the transform and trip RRSetScreenSize
    # BadMatch. Nudge the OFFSET sub-pixel (32 → 32.5 → 32), which
    # changes nothing visible (rounded to the same panel pixel) but
    # forces RRCrtcSet to refresh the shadow pixmap and CRTC scanout.
    r1 = subprocess.run(
        ["xrandr", "--fb", "640x480", "--output", "HDMI-1",
         "--transform", "0.8,0,32.5,0,0.56,16,0,0,1"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    r2 = subprocess.run(
        ["xrandr", "--fb", "640x480", "--output", "HDMI-1",
         "--transform", "0.8,0,32,0,0.56,16,0,0,1"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    dt = (time.monotonic() - t0) * 1000
    print(f"[+] crtc_renudge: {dt:.0f}ms rc={r1.returncode}/{r2.returncode}",
          flush=True)


def _open_overlay_log():
    """Open /tmp/overlay.log for append.

    Root cause story: this kernel has fs.protected_regular = 2 which
    blocks O_CREAT opens of files in /tmp owned by anyone other than
    the calling uid, even if the permission bits look fine and even
    for root. Python's standard `open(path, "ab")` ALWAYS passes
    O_CREAT, so it trips this check even when the file already exists.

    Fix: open with explicit os.open() using only O_WRONLY|O_APPEND
    (no O_CREAT). If the file is missing, create it separately with
    os.open(..., O_WRONLY|O_CREAT|O_EXCL, 0o666). Both succeed because
    they avoid the "O_CREAT on someone else's file in /tmp" pattern.
    """
    path = "/tmp/overlay.log"
    # If file doesn't exist, create it owned by us (root).
    if not os.path.exists(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        return os.fdopen(fd, "ab", buffering=0)
    # File exists: open with O_APPEND only, no O_CREAT, sidestepping
    # the fs.protected_regular check entirely.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND)
        return os.fdopen(fd, "ab", buffering=0)
    except OSError:
        # Last-resort recovery: unlink and recreate as root.
        try:
            os.unlink(path)
        except OSError:
            pass
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o666)
        return os.fdopen(fd, "ab", buffering=0)


def raise_overlay(display=":0", game="th12"):
    """Raise the tk overlay to the top of the X stack. If the overlay
    process is gone (got killed during a transition), respawn it first."""
    env = _xenv(display)
    out = subprocess.run(
        ["xdotool", "search", "--name", "^tk$"],
        capture_output=True, text=True, env=env,
    ).stdout.strip().split("\n")
    found = [w for w in out if w]
    if found:
        for w in found:
            subprocess.run(
                ["xdotool", "windowraise", w],
                env=env, stderr=subprocess.DEVNULL,
            )
        return
    # No tk window — overlay process must have died. Respawn it.
    if os.path.exists(OVERLAY_PATH):
        with _open_overlay_log() as f:
            f.write(b"=== respawn ===\n")
        subprocess.Popen(
            [OVERLAY_PATH, game],
            env=env,
            stdout=_open_overlay_log(),
            stderr=subprocess.STDOUT,
        )
        time.sleep(2.0)
        out = subprocess.run(
            ["xdotool", "search", "--name", "^tk$"],
            capture_output=True, text=True, env=env,
        ).stdout.strip().split("\n")
        for w in out:
            if w:
                subprocess.run(
                    ["xdotool", "windowraise", w],
                    env=env, stderr=subprocess.DEVNULL,
                )


def find_image_base(pid, exe):
    try:
        with open(f"/proc/{pid}/maps") as f:
            for line in f:
                if exe.lower() not in line.lower():
                    continue
                m = re.match(r"([0-9a-f]+)-[0-9a-f]+ r-xp", line)
                if m:
                    return int(m.group(1), 16)
    except (FileNotFoundError, PermissionError):
        return None
    return None


class GameMem:
    def __init__(self, pid, cfg):
        self.pid = pid
        self.cfg = cfg
        base = find_image_base(pid, cfg["exe"])
        self.delta = (base - 0x00400000) if base else 0
        # buffering=0 is REQUIRED — default 8KiB buffer caches /proc/<pid>/mem
        # so subsequent reads return stale values and the watcher never sees
        # the score climbing.
        self.fd = open(f"/proc/{pid}/mem", "r+b", buffering=0)

    def read(self, key):
        self.fd.seek(self.cfg[key] + self.delta)
        buf = self.fd.read(4)
        if len(buf) < 4:
            # /proc/<pid>/mem returned a short read — process is
            # dying / dead. Raise OSError so the watcher's main
            # loop catches it cleanly and re-attaches (instead of
            # the previous struct.error which escaped the catch
            # and killed the watcher → systemd restart loop).
            raise OSError(f"short read from /proc/{self.pid}/mem "
                          f"(got {len(buf)} bytes, want 4); pid likely dead")
        return struct.unpack("<I", buf)[0]

    def write(self, key, val):
        self.fd.seek(self.cfg[key] + self.delta)
        self.fd.write(struct.pack("<I", val))

    def score(self):
        return self.read("score") * self.cfg.get("score_mult", 1)

    def reset_score(self):
        self.write("score", 0)
        for k in ("frame", "lframe"):
            if k in self.cfg:
                self.write(k, 0)


def main():
    ap = argparse.ArgumentParser()
    # Optional game argument; currently only "th12" is supported.
    ap.add_argument("game", nargs="?", default="th12")
    ap.add_argument("--threshold", type=int, default=200000)
    ap.add_argument("--hook", default="echo GATE_EVENT")
    ap.add_argument("--interval", type=float, default=0.1)
    ap.add_argument("--display", default=":0")
    ap.add_argument("--no-lock", action="store_true",
                    help="don't xinput-disable user keyboard during reset")
    ap.add_argument("--no-flash", action="store_true",
                    help="don't show the HIT flash banner")
    ap.add_argument("--gate-window", type=float, default=30.0,
                    help="seconds to wait after threshold for the user to "
                         "hit the hit before resetting the stage")
    ap.add_argument("--gate-file", default="/tmp/score_hit",
                    help="poll this path; if it appears (touched by the "
                         "hardware hit-detect daemon), shortcut the wait")
    ap.add_argument("--no-early-disarm", action="store_true",
                    help="don't shortcut the hit window when hit-hit-file "
                         "appears. Debug visibility — keeps the gate armed "
                         "and the flash up for the full window so an "
                         "operator can observe the gate window end-to-end.")
    ap.add_argument("--state-file", default="/tmp/touhou_state",
                    help="logical kiosk state from button_daemon "
                         "(playing|hit_window|sleep). When state=sleep, "
                         "the watcher pauses its stuck-detection because "
                         "the game is intentionally paused via Esc and "
                         "the frame counter freezes by design.")
    ap.add_argument("--native-continue-diag", action="store_true",
                    help="dump screenshots to /tmp/nc_diag/ at each "
                         "substep of native_continue(). Diagnostic only.")
    ap.add_argument("--native-restart", action="store_true",
                    help="threshold path uses TH12's native stage-restart "
                         "via DLL injection (th12_restart_inject.exe + "
                         "th12_restart.dll). Falls back to systemctl "
                         "restart on helper failure.")
    ap.add_argument("--stageswitch", action="store_true",
                    help="prefer stage-switch (random stage 1..6) "
                         "over plain restart on reset. Uses "
                         "th12_stageswitch_inject.exe + th12_stageswitch.dll. "
                         "Falls back to native_restart, then systemctl restart.")
    ap.add_argument("--stageswitch-stages", default="1,2,3,4,5,6",
                    help="comma-separated list of stages to pick from when "
                         "--stageswitch is enabled. Default 1..6.")
    ap.add_argument("--threshold-only", action="store_true",
                    help="disable all auto-reset heuristics (gameover, "
                         "stuck, score-frozen at zero or non-zero). Only "
                         "resets on actual threshold cross. Useful for "
                         "debug when TH12 internal stage restarts trip "
                         "the stuck detector and cause spurious "
                         "stage_switches.")
    args = ap.parse_args()

    _install_death_diagnostics()
    print("[+] watcher pid=%d ppid=%d args=%r" % (
        os.getpid(), os.getppid(), sys.argv[1:]), flush=True)

    cfg = TH12
    pid = find_pid(cfg["exe"])
    if pid is None:
        print(f"[!] {cfg['exe']} not running", file=sys.stderr)
        return 2

    # If a previous watcher died while th12 was SIGSTOP'd (e.g. in
    # the middle of a hit window or an aborted reset), th12 stays
    # stopped forever and the kiosk freezes. SIGCONT here is a no-op
    # if the process is already running, so it's safe to always send
    # on attach.
    try:
        os.kill(pid, signal.SIGCONT)
        print(f"[+] startup SIGCONT pid={pid} (no-op if already running)",
              flush=True)
    except ProcessLookupError:
        pass

    mem = GameMem(pid, cfg)
    user_kbd = None if args.no_lock else find_user_keyboard(args.display)
    print(f"[+] watching {cfg['exe']} pid={pid} threshold={args.threshold} "
          f"user_kbd={user_kbd}", flush=True)

    last_score = 0
    last_frame = -1
    last_frame_change = time.monotonic()
    last_score_change = time.monotonic()
    cooldown_until = 0.0
    in_reset = False
    last_overlay_check = 0.0
    title_detect_start = 0.0
    # Debug-mode flag: when set, the watcher only resets on the actual
    # threshold cross. The four heuristics (gameover via lives==0,
    # score==0 stuck, score-frozen-non-zero, frame stuck) are all
    # disabled. Useful when TH12 internal stage restarts trip the
    # "stuck" detector and cause unwanted stage_switches → GPU
    # pipeline bleed. Operator sets TOUHOU_THRESHOLD_ONLY=1.
    threshold_only = (args.threshold_only
                       or bool(os.environ.get("TOUHOU_THRESHOLD_ONLY")))
    if threshold_only:
        print("[+] threshold-only mode — auto-reset heuristics disabled",
              flush=True)
    # Track recovery times so we can detect "the recovery itself
    # didn't fix the stuck state". If the same trigger fires again
    # within the escalation window, the recovery isn't working and
    # we escalate to systemctl restart. Common case: continues
    # exhausted → game on title screen → native_continue Z lands on
    # "Game Start" → goes to Rank Select (no in-game frame ticks) →
    # frame-stuck fires again forever. Escalation breaks the loop.
    last_score_frozen_reset = 0.0
    last_score_frozen_count = 0
    last_stuck_reset = 0.0



    def read_kiosk_state():
        """Returns 'playing', 'hit_window', 'sleep', or 'playing' on any
        error. State file is rename-atomic on the writer side, so a
        plain read is safe."""
        try:
            with open(args.state_file) as f:
                v = f.read().strip()
                if v in ("playing", "hit_window", "sleep"):
                    return v
        except (FileNotFoundError, OSError):
            pass
        return "playing"
    pause_attempts = 0  # consecutive Esc dismissals; after 3 → escalate

    def do_reset(reason):
        """All reset paths converge on systemctl restart of the kiosk
        service — only that's reliable through Wine's keyboard grab.

        reason == "threshold" → user reached score gate. Fire hit hook,
            flash "HIT", pause the game (Esc), lock keyboard, wait up to
            args.hit_window seconds for either a hit-file to appear OR
            the timeout to elapse, then restart.
        reason == "gameover"  → player ran out of lives: silent restart,
            no hook, no flash, no wait.
        reason == "stuck"     → frame counter frozen and Esc didn't help:
            silent restart, no hook, no flash, no wait. Important to NOT
            fire the hook here because boss intros / spell-card
            declarations look like pause to the watcher.
        """
        nonlocal cooldown_until, in_reset
        in_reset = True
        # Tell score_overlay to switch to load mode (Loading text on
        # full-playfield mask). For non-threshold resets we set this
        # IMMEDIATELY because the destructive work starts right away.
        # For THRESHOLD resets the HIT flash is the user-facing UX
        # and we defer the resetting flag until after the hit window
        # ends — otherwise the user sees a brief "Loading..." flash
        # before HIT which is ugly.
        if reason != "threshold":
            try:
                open("/tmp/touhou_resetting", "w").close()
            except OSError:
                pass
        try:
            print(f"[!] reset triggered: {reason}", flush=True)
            # Force our overlays back on top of any wine windows that
            # spawned. raise_overlay() walks all "tk"-named windows and
            # XRaise's them — needed because bare X has no compositor
            # and tk's "-topmost" attribute is just a hint that nothing
            # is enforcing.
            raise_overlay(display=args.display, game=args.game)
            # Brief settle: give score_overlay a tick to react to the
            # resetting flag (it polls at 100ms) and expand its mask
            # to the full 640x480 wine vdesk before we tear down wine.
            # Without this gap, the destructive work below can race
            # the mask's appearance — the panel briefly shows wine's
            # last frame before the mask covers it.
            time.sleep(0.2)
            # xrefresh sends Expose to every X window, nudging clients
            # to repaint. By itself this only updates the X server
            # primary fb; we ALSO need to force the CRTC to drop any
            # references to wine's DMA-BUF and re-blit from primary fb.
            subprocess.run(["xrefresh", "-display", args.display],
                           env=_xenv(args.display),
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            # Transform-nudge: re-issue the playfield-crop xrandr
            # transform with a 1-tick-different value, then immediately
            # back. modesetting short-circuits identical transforms, so
            # we have to differ by at least one float bit. This triggers
            # RRCrtcSet → shadow pixmap re-creation → glamor CopyArea
            # of the full screen into a NEW scanout BO. The old BO
            # (which may have been a wine DMA-BUF) is dropped from the
            # CRTC. Cheap (~10ms), no HDMI re-link, no visible flicker
            # because our mask is already up. 
            _crtc_renudge(args.display)
            if user_kbd is not None:
                lock_keyboard(user_kbd, args.display)
            if reason == "threshold":
                # Clear any stale hit file before the hook starts the gate
                # bridge. gate_bridge.py treats this file as an early-disarm
                # signal, so clearing it after HOOK races and can collapse the
                # new hit window immediately if a prior debug/button run left
                # the file behind.
                try:
                    os.unlink(args.gate_file)
                except FileNotFoundError:
                    pass
                except OSError as e:
                    print(f"[!] could not clear hit file before hook: {e}",
                          file=sys.stderr)
                # Fire the hook (logs pulse / GPIO unlock)
                try:
                    subprocess.run(args.hook, shell=True, check=False, timeout=5)
                except Exception as e:
                    print(f"[!] hook failed: {e}", file=sys.stderr)
                # Pause TH12 during the hit window via Esc (its native
                # pause shortcut). Esc is safer than SIGSTOP because it
                # doesn't freeze wine's d3d9 device state.
                _prime_focus(args.display)
                time.sleep(0.3)
                subprocess.run(["xdotool", "key", "Escape"],
                               env=_xenv(args.display))
                # HIT flash for the entire hit-window duration. Returns
                # a Popen handle so we can kill it early if the user
                # signals a hit and we shortcut to restart.
                flash_proc = None
                if not args.no_flash:
                    flash_proc = show_hit_flash(
                        args.display, duration=args.hit_window,
                        gate_file=args.gate_file,
                    )
                # Wait up to hit_window seconds for the hit-file to
                # appear (touched by a hardware hit-detect daemon when
                # wired up). Polls every 0.2s.
                print(f"[+] hit window {args.hit_window:.0f}s "
                      f"(hit file: {args.gate_file})", flush=True)
                deadline = time.monotonic() + args.hit_window
                hit = False
                while time.monotonic() < deadline:
                    if os.path.exists(args.gate_file):
                        hit = True
                        # In normal mode, break early — the user pressed
                        # the helper button, restart_stage as soon as
                        # possible. In debug (--no-early-disarm), keep
                        # waiting so the gate window is visible end-to-end.
                        if not args.no_early_disarm:
                            break
                    time.sleep(0.2)
                print(f"[+] hit window done (hit={hit})", flush=True)
                # Kill the flash window before tearing down
                if flash_proc is not None and flash_proc.poll() is None:
                    flash_proc.terminate()
                    try:
                        flash_proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        flash_proc.kill()
                try:
                    os.unlink(args.gate_file)
                except OSError:
                    pass
                # Hit UX done — NOW raise the Loading mask. Until this
                # point the only mask on screen was the HIT flash's
                # own opaque black canvas + the load_win black bg with
                # no text (driven by /tmp/hit_active). With hit gone,
                # the user would briefly see wine again before the
                # destructive reset work below; the resetting flag
                # pulls Loading text up to cover that gap.
                try:
                    open("/tmp/touhou_resetting", "w").close()
                except OSError:
                    pass
            # Reset path:
            #   reason == "threshold" — player succeeded, fire the hook,
            #     then stage_switch / native_restart / systemctl.
            #   reason == "gameover" or "stuck" — game-over screen or
            #     pause menu visible. native_continue sends Z directly
            #     to the game window via xdotool --window targeting,
            #     which uses TH12's NATIVE Continue option (default-
            #     highlighted on game-over). Game restarts current
            #     stage. No kiosk restart, no menus after a frame.
            restored = False
            if reason == "threshold":
                # Threshold path reset ladder:
                #   1. stage-switch to a random stage (primary path) — IF
                #      --stageswitch is enabled. This is the primary
                #      kiosk reset because it both restarts the stage
                #      AND randomizes which stage comes next.
                #   2. native_restart — same-stage restart via DLL injection.
                #   3. systemctl restart — bombproof fallback.
                # Each rung is independent; if a higher rung fails we
                # fall through to the next.
                stages_enabled = args.stageswitch or os.environ.get(
                    "TH12_STAGESWITCH")
                if stages_enabled:
                    pool = [int(s.strip()) for s
                            in args.stageswitch_stages.split(",")
                            if s.strip().isdigit() and 1 <= int(s.strip()) <= 6]
                    if not pool:
                        pool = [1, 2, 3, 4, 5, 6]
                    # 2-stage pool → alternate deterministically (pick the
                    # stage that isn't the current one).
                    if len(pool) == 2:
                        try:
                            cur_stage = mem.read("stage")
                        except OSError:
                            cur_stage = 0
                        candidates = [s for s in pool if s != cur_stage]
                        target = candidates[0] if candidates else pool[0]
                    else:
                        target = random.choice(pool)
                    print(f"[+] stageswitch chosen target={target} "
                          f"(pool={pool})", flush=True)
                    restored = th12_stageswitch_via_wine(
                        target, display=args.display)
                    if not restored:
                        print("[!] stageswitch failed; falling back to "
                              "native_restart", flush=True)
                if not restored and (
                        args.native_restart or os.environ.get(
                            "TH12_NATIVE_RESTART")):
                    restored = th12_restart_via_wine(display=args.display)
                    if not restored:
                        print("[!] native restart failed; falling back to "
                              "systemctl restart", flush=True)
            elif reason in ("gameover", "stuck", "title_screen"):
                # Same reset ladder as threshold but no hook fire.
                stages_enabled = args.stageswitch or os.environ.get(
                    "TH12_STAGESWITCH")
                if stages_enabled:
                    pool = [int(s.strip()) for s
                            in args.stageswitch_stages.split(",")
                            if s.strip().isdigit() and 1 <= int(s.strip()) <= 6]
                    if not pool:
                        pool = [1, 2, 3, 4, 5, 6]
                    if len(pool) == 2:
                        try:
                            cur_stage = mem.read("stage")
                        except OSError:
                            cur_stage = 0
                        candidates = [s for s in pool if s != cur_stage]
                        target = candidates[0] if candidates else pool[0]
                    else:
                        target = random.choice(pool)
                    print(f"[+] stageswitch chosen target={target} "
                          f"(reason={reason} pool={pool})", flush=True)
                    restored = th12_stageswitch_via_wine(
                        target, display=args.display)
                if not restored and (
                        args.native_restart or os.environ.get(
                            "TH12_NATIVE_RESTART")):
                    restored = th12_restart_via_wine(display=args.display)
                if not restored:
                    # native_continue (Z) handles menu dismissal via
                    # TH12's own Continue mechanism. Empirically works
                    # on game-over screen (frame goes ~3000 → ~110, real
                    # stage restart). On title (continues exhausted),
                    # both Z and Esc+Up+Z fail → restored stays False →
                    # fall through to restart_stage() (systemctl).
                    restored = native_continue(
                        display=args.display, pid=pid,
                        diag=args.native_continue_diag)
            elif reason == "score_frozen":
                if args.native_restart or os.environ.get("TH12_NATIVE_RESTART"):
                    restored = th12_restart_via_wine(display=args.display)
            if not restored:
                restart_stage(display=args.display)
                # The systemctl restart kills *this* watcher process.
                # Anything below is unreachable; the new launcher will spawn
                # a fresh watcher attached to the new game.
        finally:
            if user_kbd is not None:
                unlock_keyboard(user_kbd, args.display)
            # 8 s cooldown post-reset: gives the game time to settle
            # into a new stage's first frame before we re-arm the
            # threshold / heuristic detection. Longer would leave the
            # kiosk visibly frozen; shorter risks re-triggering a reset
            # against the stage-switch transient.
            cooldown_until = time.monotonic() + 8.0
            in_reset = False
            # Clear the resetting flag ourselves as a safety net. The
            # design relies on score_overlay observing the frame-counter
            # cycle (low → high) to know when reset is complete, but if
            # score_overlay dies, the flag would stay stuck forever and
            # the next reset would see it pre-set. The actual mask logic
            # in score_overlay also keys off frame<30 → load mode, so
            # clearing the flag here doesn't drop the mask early — the
            # frame-based check holds it until gameplay actually resumes.
            try:
                os.unlink("/tmp/touhou_resetting")
            except FileNotFoundError:
                pass
            except OSError as e:
                print(f"[!] could not clear resetting flag: {e}",
                      file=sys.stderr)

    while True:
        try:
            s = mem.score()
            f = mem.read("frame")
            stage = mem.read("stage")
            lives = mem.read("lives") if "lives" in cfg else None
        except OSError as e:
            print(f"[!] read failed: {e}; re-attaching", file=sys.stderr)
            time.sleep(1.0)
            new_pid = find_pid(cfg["exe"])
            if new_pid is None:
                print("[!] game gone, exiting", file=sys.stderr)
                return 1
            mem = GameMem(new_pid, cfg)
            pid = new_pid
            last_score = 0
            last_frame = -1
            continue

        now = time.monotonic()

        if f != last_frame:
            last_frame = f
            last_frame_change = now
            pause_attempts = 0
        if s != last_score:
            print(f"score={s} stage={stage} lives={lives} frame={f}",
                  flush=True)
            last_score = s
            last_score_change = now

        # Periodic overlay liveness check: if score_overlay died (e.g.
        # X server hiccup, tk crash, OOM kill, operator kill), it won't
        # be there to mask the next reset bleedover. raise_overlay
        # respawns if no tk window is found. Throttle to every 30s so
        # we're not banging xdotool every tick.
        if now - last_overlay_check > 30.0:
            raise_overlay(display=args.display, game=args.game)
            last_overlay_check = now

        if in_reset or now < cooldown_until:
            time.sleep(args.interval)
            continue

        # Title-screen / game-over-screen detector. When the player
        # finishes a run and TH12 unwinds the stage flow back to title,
        # the stage_struct_ptr at 0x4B44E8 goes NULL (stage struct is
        # deallocated). The `stage` global stays at the last-played
        # value, so that's NOT a reliable signal — but sptr==0 is.
        # Bypasses --threshold-only because it's unambiguous: sptr==0
        # during gameplay never happens.
        try:
            sptr = mem.read("sptr")
        except OSError:
            sptr = -1  # transient read failure; don't tally
        if sptr == 0:
            if title_detect_start == 0.0:
                title_detect_start = now
            elif now - title_detect_start > 5.0:
                print(f"[!] title-screen detected (sptr=0 for "
                      f"{now - title_detect_start:.0f}s); "
                      f"systemctl restart to recover", flush=True)
                # stage_switch DLL won't work here (its precondition
                # check bails when sptr==0). Use the heavy hammer:
                # systemctl restart kicks the kiosk back through
                # cold-boot menu_nav into a fresh stage.
                restart_stage(display=args.display)
                # restart_stage triggers systemctl which kills this
                # watcher; nothing below runs
        elif sptr > 0:
            title_detect_start = 0.0

        # Threshold crossed → fire hit + restart
        if s >= args.threshold:
            do_reset("threshold")
            last_score = 0
            last_score_change = time.monotonic()
            last_frame_change = time.monotonic()
            time.sleep(args.interval)
            continue

        # Game-over heuristic: in stage > 0, lives at zero, frame counter
        # has stopped advancing for >2s. Auto-press Continue so the player
        # gets back into stage 1 instead of being stuck on the game-over
        # screen forever (TH12 doesn't time out by itself).
        # TH12 leaves the lives word at its last in-stage value on the
        # game-over screen rather than zeroing it, so this branch is a
        # fallback; the stuck branch below handles the common case via
        # frame-frozen-+-failed-Esc detection.
        if (not threshold_only
                and lives is not None and lives == 0 and stage > 0
                and now - last_frame_change > 2.0):
            do_reset("gameover")
            last_score = 0
            last_frame = -1
            last_score_change = time.monotonic()
            last_frame_change = time.monotonic()
            time.sleep(args.interval)
            continue

        # When the kiosk is in `sleep` state, the game is paused via Esc
        # by button_daemon and the frame counter freezes by design — do
        # NOT stuck-detect it. Same reasoning for score: score-frozen
        # detection must also yield during sleep.
        kiosk_state = read_kiosk_state()
        if kiosk_state == "sleep":
            last_frame_change = now
            last_score_change = now
            pause_attempts = 0
            time.sleep(args.interval)
            continue

        # Score-stuck-at-zero heuristic: in stage > 0, score == 0 for
        # >30s. Catches pause-menu / continue-prompt / title-screen
        # states where the frame counter still ticks (idle animations)
        # but score is locked at 0. score==0 is the explicit signal —
        # not score-frozen-at-any-value (which would false-trigger
        # during quiet stretches of legitimate play).
        # Threshold 30s gives Reimu plenty of time to clear the first
        # stage 1 enemy wave (which awards score) before we conclude
        # the kiosk is stuck.
        # If this fires twice within 60s, the stage-switch / native
        # restart isn't fixing the state — escalate to systemctl restart.
        if (not threshold_only
                and lives is not None and lives > 0 and stage > 0 and s == 0
                and now - last_score_change > 90.0):
            t_since_prev = now - last_score_frozen_reset
            if last_score_frozen_reset > 0 and t_since_prev < 60.0:
                print(f"[!] score=0 stuck again {t_since_prev:.0f}s after "
                      f"prev recovery; escalating to systemctl restart",
                      flush=True)
                restart_stage(display=args.display)
                # restart_stage triggers systemctl which kills this
                # watcher; nothing below here runs
            print(f"[!] score=0 stuck >30s; treating as menu/pause",
                  flush=True)
            do_reset("score_frozen")
            last_score = 0
            last_frame = -1
            # Reset clocks AFTER the reset completes so we don't
            # immediately re-fire on the next loop iteration.
            last_score_change = time.monotonic()
            last_score_frozen_reset = time.monotonic()
            time.sleep(args.interval)
            continue

        # Score-frozen-at-non-zero heuristic: stage > 0, score holds a
        # non-zero value for >45s, frame still ticking. Catches the
        # post-death game-over screen, continue prompts, and other menu
        # states where the frame counter keeps advancing (idle/menu
        # animations) but no points can accrue. TH12's `lives` counter
        # is unreliable here (preserves last-played value on game-over,
        # so the lives==0 branch above rarely fires) and the stuck
        # branch below also misses this because frame isn't actually
        # frozen — the game-over screen has its own animation loop.
        #
        # 45s threshold: long enough not to false-trigger on slow play
        # / hiding in safe zones between waves; short enough to detect
        # a genuinely stalled run quickly.
        if (not threshold_only
                and lives is not None and lives > 0 and stage > 0 and s > 0
                and now - last_score_change > 45.0):
            t_since_prev = now - last_score_frozen_reset
            # Escalation gap is 180s: only escalate to systemctl restart
            # if THREE+ rapid score_frozen events stack up. A 45s trigger
            # avoids false positives during normal quiet stage openings but
            # while still allowing one-off false positives to clear.
            if last_score_frozen_reset > 0 and t_since_prev < 180.0 \
                    and last_score_frozen_count >= 2:
                print(f"[!] score frozen non-zero {last_score_frozen_count+1}x "
                      f"within 180s; escalating to systemctl restart",
                      flush=True)
                restart_stage(display=args.display)
            print(f"[!] score frozen at {s} for {now - last_score_change:.0f}s "
                  f"(stage {stage}, lives {lives}) — likely game-over/menu",
                  flush=True)
            do_reset("score_frozen")
            # Count repeat triggers within the 180s escalation window;
            # reset the count if it's been longer than the window.
            if t_since_prev < 180.0:
                last_score_frozen_count += 1
            else:
                last_score_frozen_count = 1
            last_score = 0
            last_frame = -1
            last_score_change = time.monotonic()
            last_score_frozen_reset = time.monotonic()
            time.sleep(args.interval)
            continue

        # Pause heuristic: in stage > 0, lives > 0, frame frozen for >10s.
        # 10s avoids false-triggering on stage 1 boss intros and spell-
        # card declarations (~3-5s each) while still catching genuine
        # paused-by-user states. Try Esc up to 3 times; if Esc can't
        # unfreeze the frame counter, escalate to a silent service
        # restart (does NOT fire the hit hook — this is recovery, not
        # a threshold cross).
        # This branch is also the de-facto TH12 game-over recovery path
        # (see lives==0 note above).
        if (not threshold_only
                and lives is not None and lives > 0 and stage > 0
                and now - last_frame_change > 10.0):
            # Stuck recovery: native_continue (Z press) handles the
            # common case (game-over screen) without opening TH12's
            # pause menu via Esc — pressing Z just confirms TH12's
            # default-highlighted "Continue".
            if True:
                # Escalation: if a previous stuck reset finished within
                # the last 30s and frame is stuck again, the recovery
                # isn't working (typically: game on title screen, Z
                # confirms wrong menu option, never reaches gameplay).
                # Skip retrying and go straight to systemctl restart.
                t_since_prev = time.monotonic() - last_stuck_reset
                if last_stuck_reset > 0 and t_since_prev < 30.0:
                    print(f"[!] stuck again {t_since_prev:.0f}s after prev "
                          f"recovery; escalating to systemctl restart",
                          flush=True)
                    restart_stage(display=args.display)
                    # systemctl restart kills this process
                print("[!] frame stuck >10s; native_continue (Z to game window)",
                      flush=True)
                do_reset("stuck")
                last_stuck_reset = time.monotonic()
                pause_attempts = 0
                last_score = 0
                last_frame = -1
                last_score_change = time.monotonic()
                time.sleep(args.interval)
                continue
            if pause_attempts >= 3:
                print(f"[!] {pause_attempts} pause attempts failed — silent restart",
                      flush=True)
                do_reset("stuck")
                pause_attempts = 0
                last_score = 0
                last_frame = -1
                last_score_change = time.monotonic()
                time.sleep(args.interval)
                continue
            pause_attempts += 1
            print(f"[!] suspected pause (frame stuck @{f}, lives={lives}) "
                  f"attempt {pause_attempts}/3 — Esc", flush=True)
            _prime_focus(args.display)
            time.sleep(0.2)
            subprocess.run(["xdotool", "key", "Escape"],
                           env=_xenv(args.display))
            # Push the "last frame change" timestamp 3 s into the
            # FUTURE so the stuck-detect won't re-fire for at
            # least 3 s — gives the Esc keypress time to land and
            # the engine time to unpause without our retry loop
            # racing it. Misnamed variable but the offset matters:
            # the comparison is `now - last_frame_change > 10.0`,
            # and we're effectively setting "give it 13 s before
            # re-firing" by stashing now+3 here.
            last_frame_change = now + 3.0
            time.sleep(args.interval)
            continue

        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main() or 0)

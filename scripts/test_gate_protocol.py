#!/usr/bin/env python3
"""
Gate-protocol regression tests for the software gate stub.

Spawns its own fake_gate_mcu.py on a temp Unix socket, then exercises
every command in the ASCII line protocol (PING / STATUS / ARM /
HEARTBEAT / DISARM) plus auto-disarm by deadman timeout and by window
expiry, plus default-deny edge cases. Prints pass/fail per vector and
exits 0 only if all pass.

Usage:
    ./scripts/test_gate_protocol.py
    ./scripts/test_gate_protocol.py --keep-running    # leave fake MCU up

Exit codes:
    0  all vectors passed
    1  one or more vectors failed
    2  could not start fake MCU
"""
import argparse
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _find_fake_mcu():
    """Find fake_gate_mcu.py — works in both repo layout (this script in
    REPO/scripts/) and deployed layout (this script and fake_gate_mcu.py
    side by side in /home/ubuntu/touhou-kiosk/)."""
    candidates = [
        os.path.join(SCRIPT_DIR, "fake_gate_mcu.py"),
        os.path.join(os.path.dirname(SCRIPT_DIR), "scripts", "fake_gate_mcu.py"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return candidates[0]  # fall through; main() will report the error


FAKE_MCU = _find_fake_mcu()


class Client:
    """Open one Unix-socket connection and send/receive line protocol."""
    def __init__(self, sock_path, timeout=2.0):
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.settimeout(timeout)
        self.s.connect(sock_path)
        self.f = self.s.makefile("rwb", buffering=0)

    def cmd(self, line):
        self.f.write((line + "\n").encode())
        return self.f.readline().decode("utf-8", errors="replace").rstrip()

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass


def vector(name, send, expect_prefix, expect_contains=None):
    """Returns (passed, response, message)."""
    return (name, send, expect_prefix, expect_contains or [])


VECTORS = [
    # (name, send, expect_starts_with, expect_contains)
    vector("ping", "PING", "OK PONG"),
    vector("status_default_disarmed",
           "STATUS", "OK STATE armed=False"),
    vector("heartbeat_while_disarmed_errs",
           "HEARTBEAT", "ERR not_armed"),
    vector("arm_negative_rejected",
           "ARM -1", "ERR window_out_of_range"),
    vector("arm_zero_rejected",
           "ARM 0", "ERR window_out_of_range"),
    vector("arm_too_long_rejected",
           "ARM 700", "ERR window_out_of_range"),
    vector("arm_non_numeric_rejected",
           "ARM banana", "ERR bad_seconds"),
    vector("arm_missing_arg_rejected",
           "ARM", "ERR usage:"),
    vector("unknown_verb_rejected",
           "FOO", "ERR unknown_verb FOO"),
    vector("disarm_idempotent_when_disarmed",
           "DISARM", "OK DISARMED"),
    vector("arm_valid",
           "ARM 5", "OK ARMED expires_at="),
    vector("status_after_arm",
           "STATUS", "OK STATE armed=True"),
    vector("heartbeat_while_armed",
           "HEARTBEAT", "OK ALIVE remaining="),
    vector("disarm_while_armed",
           "DISARM", "OK DISARMED"),
    vector("status_after_disarm",
           "STATUS", "OK STATE armed=False"),
    vector("heartbeat_again_disarmed",
           "HEARTBEAT", "ERR not_armed"),
]


def wait_for_socket(path, timeout=5.0):
    """Poll until socket appears + accepts a connection."""
    end = time.time() + timeout
    while time.time() < end:
        if os.path.exists(path):
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(0.5)
                s.connect(path)
                s.close()
                return True
            except OSError:
                pass
        time.sleep(0.05)
    return False


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--keep-running", action="store_true",
                   help="don't kill the fake MCU on exit")
    p.add_argument("--hb-timeout-ms", type=int, default=1500,
                   help="passed to fake_gate_mcu --hb-timeout-ms")
    p.add_argument("--mcu", default=FAKE_MCU,
                   help="path to fake_gate_mcu.py")
    args = p.parse_args()

    if not os.path.exists(args.mcu):
        print("ERR fake_gate_mcu.py not found at %s" % args.mcu,
              file=sys.stderr)
        return 2

    tmp_sock = tempfile.mktemp(prefix="touhou_gate_test_", suffix=".sock")
    tmp_log = tempfile.mktemp(prefix="touhou_gate_test_", suffix=".log")

    proc = subprocess.Popen(
        [sys.executable, args.mcu,
         "--socket", tmp_sock,
         "--hb-timeout-ms", str(args.hb_timeout_ms),
         "--log", tmp_log],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )

    if not wait_for_socket(tmp_sock, timeout=5.0):
        print("ERR fake MCU didn't open socket within 5s", file=sys.stderr)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except OSError:
            pass
        return 2

    failures = 0
    print("=== gate protocol regression ===")
    print("socket: %s" % tmp_sock)
    print("log:    %s" % tmp_log)
    print()
    try:
        c = Client(tmp_sock)
        for name, send, expect_prefix, expect_contains in VECTORS:
            try:
                got = c.cmd(send)
            except Exception as e:
                got = "EXCEPTION %r" % e
            ok = got.startswith(expect_prefix) and all(
                x in got for x in expect_contains)
            mark = "PASS" if ok else "FAIL"
            print("%s  %-40s  send=%r  got=%r" % (mark, name, send, got))
            if not ok:
                failures += 1
                print("       expected prefix: %r" % expect_prefix)
                if expect_contains:
                    print("       expected substrings: %r" % expect_contains)
        c.close()

        # Auto-disarm by deadman timeout (no heartbeats sent for >hb-timeout)
        # Use a short timeout for fast test.
        print()
        print("=== auto-disarm (deadman timeout) ===")
        c2 = Client(tmp_sock)
        c2.cmd("ARM 60")  # long window
        time.sleep((args.hb_timeout_ms / 1000.0) + 0.5)  # exceed deadman
        got = c2.cmd("STATUS")
        ok = "armed=False" in got
        mark = "PASS" if ok else "FAIL"
        print("%s  deadman_auto_disarm  got=%r" % (mark, got))
        if not ok:
            failures += 1
        c2.close()

        # Auto-disarm by window expiry
        print()
        print("=== auto-disarm (window expiry) ===")
        c3 = Client(tmp_sock)
        c3.cmd("ARM 1")
        # Heartbeat to keep deadman alive but let window expire
        c3.cmd("HEARTBEAT")
        time.sleep(0.5)
        c3.cmd("HEARTBEAT")
        time.sleep(0.7)  # past the 1s window
        got = c3.cmd("STATUS")
        ok = "armed=False" in got
        mark = "PASS" if ok else "FAIL"
        print("%s  window_expiry  got=%r" % (mark, got))
        if not ok:
            failures += 1
        c3.close()

    finally:
        if not args.keep_running:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
            try:
                os.unlink(tmp_sock)
            except OSError:
                pass

    print()
    if failures == 0:
        print("ALL PASS")
        return 0
    print("FAILED %d vector(s)" % failures)
    return 1


if __name__ == "__main__":
    sys.exit(main())

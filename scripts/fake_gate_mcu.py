#!/usr/bin/env python3
"""
Gate daemon — software state-machine for the score watcher's hook.

Speaks a simple ASCII line protocol over a unix socket. Sits between
the kiosk and whatever the hook is configured to eventually drive.
On its own, this daemon doesn't *do* anything physical — it owns the
safety state machine (default deny, heartbeat deadman, single arm
window) and refuses to leave its permissive window open. If you wire
the hook to a real actuator, the safety properties this daemon
enforces are what protect against the watcher crashing mid-window.

What it simulates accurately for a real actuator:
  - "permit window opens for N seconds, then closes."
  - Default-deny on every failure mode.
  - Auto-disarm on a deadman heartbeat timeout.

Default-deny invariants enforced:
  - boots DISARMED
  - only ARM <seconds> transitions to ARMED
  - auto-DISARM on window expiry
  - auto-DISARM on heartbeat timeout (deadman, default 1500ms)
  - HEARTBEAT while DISARMED returns ERR (does NOT silently re-arm)

Every event is logged to stderr and (if --log given) appended to a
file, ISO timestamp + verb.
"""
import argparse
import os
import socket
import sys
import threading
import time
import signal
from datetime import datetime, timezone


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Gate:
    def __init__(self, hb_timeout_ms, log_path, audit_path=None):
        self.armed = False
        self.window_expires = 0.0
        self.last_heartbeat = 0.0
        self.hb_timeout = hb_timeout_ms / 1000.0
        self.log_path = log_path
        self.audit_path = audit_path
        self.lock = threading.Lock()
        self.log("BOOT default=DISARMED hb_timeout_ms=%d" % hb_timeout_ms)

    def log(self, msg):
        line = "[%s] %s" % (_now_iso(), msg)
        print(line, file=sys.stderr, flush=True)
        if self.log_path:
            try:
                with open(self.log_path, "a") as f:
                    f.write(line + "\n")
            except Exception as e:
                print("[%s] LOG_WRITE_FAIL %r" % (_now_iso(), e),
                      file=sys.stderr, flush=True)

    def audit(self, msg):
        """Write a one-line audit entry to the SBC-side audit log. Used to
        fill the GATE_DISARM gap when the kiosk restart kills the bg
        gate_bridge child before it can write its own DISARM line. The
        fake MCU is owned by systemd (root), survives kiosk restarts, and
        is still alive to record the deadman-driven auto-disarm."""
        if not self.audit_path:
            return
        line = "[%s] %s" % (_now_iso(), msg)
        try:
            with open(self.audit_path, "a") as f:
                f.write(line + "\n")
        except Exception as e:
            self.log("AUDIT_WRITE_FAIL %r" % e)

    def state_tuple(self, now=None):
        if now is None:
            now = time.time()
        if not self.armed:
            return (False, 0.0, 0.0)
        remaining = max(0.0, self.window_expires - now)
        hb_age = now - self.last_heartbeat
        return (True, remaining, hb_age)

    def tick(self):
        """Called periodically by the ticker thread. Enforces auto-disarm."""
        with self.lock:
            if not self.armed:
                return
            now = time.time()
            if now > self.window_expires:
                self.log("AUTO_DISARM reason=window_expired")
                # window_expired: heartbeats were arriving (otherwise deadman
                # would have fired first), so the bridge is alive and will
                # write its own GATE_DISARM. Don't double-write here.
                self.armed = False
                return
            if now - self.last_heartbeat > self.hb_timeout:
                self.log("AUTO_DISARM reason=deadman_timeout")
                # deadman_timeout: bridge went silent. It either died (a
                # kiosk restart can kill it) or hung. Either way it won't
                # write its DISARM audit line. We do it on its behalf so
                # the audit log doesn't have a dangling GATE_ARM entry.
                self.audit("GATE_DISARM reason=deadman_timeout source=fake_gate_mcu")
                self.armed = False

    def cmd(self, line):
        with self.lock:
            now = time.time()
            parts = line.strip().split()
            if not parts:
                return "ERR empty"
            verb = parts[0].upper()

            if verb == "PING":
                return "OK PONG"

            if verb == "ARM":
                if len(parts) != 2:
                    return "ERR usage: ARM <seconds>"
                try:
                    w = float(parts[1])
                except ValueError:
                    return "ERR bad_seconds"
                if w <= 0 or w > 600:
                    return "ERR window_out_of_range"
                self.armed = True
                self.window_expires = now + w
                self.last_heartbeat = now
                self.log("ARM window=%.3fs expires_in=%.3fs" % (w, w))
                return "OK ARMED expires_at=%.3f" % self.window_expires

            if verb == "HEARTBEAT":
                if not self.armed:
                    return "ERR not_armed"
                self.last_heartbeat = now
                _, remaining, _ = self.state_tuple(now)
                return "OK ALIVE remaining=%.1f" % remaining

            if verb == "DISARM":
                if self.armed:
                    self.log("DISARM reason=requested")
                    self.armed = False
                return "OK DISARMED"

            if verb == "STATUS":
                armed, remaining, hb_age = self.state_tuple(now)
                return ("OK STATE armed=%s remaining=%.1f hb_age=%.2f"
                        % (str(armed), remaining, hb_age))

            return "ERR unknown_verb %s" % verb


def serve(sock_path, gate):
    if os.path.exists(sock_path):
        os.unlink(sock_path)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(sock_path)
    os.chmod(sock_path, 0o666)
    s.listen(8)
    gate.log("LISTEN socket=%s" % sock_path)

    def ticker():
        while True:
            time.sleep(0.1)
            gate.tick()
    t = threading.Thread(target=ticker, daemon=True)
    t.start()

    while True:
        conn, _ = s.accept()
        threading.Thread(
            target=handle_client, args=(conn, gate), daemon=True
        ).start()


def handle_client(conn, gate):
    try:
        f = conn.makefile("rwb", buffering=0)
        for raw in iter(f.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip()
            if not line:
                continue
            # HEARTBEAT fires every ~500ms during a gate window, dwarfing
            # ARM/DISARM in the log. Skip the per-heartbeat RX/TX lines and
            # rely on the ARM/AUTO_DISARM/DISARM events for the audit story.
            verb = line.split(None, 1)[0].upper() if line else ""
            if verb != "HEARTBEAT":
                gate.log("RX %s" % line)
            resp = gate.cmd(line)
            if verb != "HEARTBEAT":
                gate.log("TX %s" % resp)
            try:
                f.write((resp + "\n").encode())
            except Exception:
                break
    except Exception as e:
        gate.log("CLIENT_ERROR %r" % e)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--socket", default="/tmp/touhou_gate.sock")
    p.add_argument("--hb-timeout-ms", type=int, default=1500,
                   help="auto-disarm if no heartbeat in this window while armed")
    p.add_argument("--log", default="/tmp/fake_gate_mcu.log")
    p.add_argument("--audit-file", default="/tmp/gate_audit.log",
                   help="append a GATE_DISARM line here when AUTO_DISARM "
                        "fires due to deadman_timeout. Fills the audit gap "
                        "when the kiosk restart kills the bg gate_bridge "
                        "before it can write its own DISARM line.")
    args = p.parse_args()

    g = Gate(args.hb_timeout_ms, args.log, audit_path=args.audit_file)
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    signal.signal(signal.SIGINT, lambda *a: sys.exit(0))
    serve(args.socket, g)


if __name__ == "__main__":
    main()

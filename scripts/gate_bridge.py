#!/usr/bin/env python3
"""
Gate bridge CLI — OPi-side talker for the gate daemon over a unix socket.

Used as the score-watcher's HOOK in /etc/default/touhou-kiosk:
    HOOK='/home/ubuntu/touhou-kiosk/gate_bridge.py arm-window 5 --background'

Speaks a simple ASCII line protocol (ARM / DISARM / HEARTBEAT / STATUS).
The companion `fake_gate_mcu.py` is the gate daemon — together they
implement the watcher's "fire the hook + hold the window + auto-disarm
on deadman" pattern.

Sub-commands:
    ping                              connectivity test
    status                            print STATUS line
    arm <seconds>                     ARM only, return immediately
    disarm                            DISARM
    heartbeat                         single HEARTBEAT
    arm-window <seconds>              ARM, hold heartbeat until window expires
                                      OR --gate-file appears, then DISARM.
                                      With --background, fork+detach so the
                                      caller (HOOK) returns immediately.

Exit codes:
    0  success
    1  protocol error from MCU
    2  socket error / MCU unreachable
"""
import argparse
import os
import socket
import sys
import time
from datetime import datetime, timezone


_AUDIT_WARNED = False  # only complain once per process


def _audit(args, msg):
    """Append an ISO-timestamped audit line to args.audit_file. On any
    write failure (e.g. file owned by another user), print a warning to
    stderr ONCE per process so the failure isn't silent — silent gaps in
    the audit log would be misleading. Production HOOK runs as root and
    writes succeed; this only matters for manual debug invocations."""
    global _AUDIT_WARNED
    if not args.audit_file:
        return
    ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    try:
        with open(args.audit_file, "a") as f:
            f.write("[%s] %s\n" % (ts, msg))
    except OSError as e:
        if not _AUDIT_WARNED:
            print(
                "[%s] AUDIT_WRITE_FAILED file=%s err=%r — audit lines from "
                "this process are dropped. Run with sudo, or pass "
                "--audit-file PATH to a writable location." % (
                    ts, args.audit_file, e),
                file=sys.stderr,
            )
            _AUDIT_WARNED = True


def open_conn(path, timeout=2.0):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(path)
    except OSError as e:
        print("ERR connect %s: %s" % (path, e), file=sys.stderr)
        sys.exit(2)
    return s


def send_line(sock, line):
    sock.sendall((line + "\n").encode())
    f = sock.makefile("rb")
    return f.readline().decode("utf-8", errors="replace").rstrip()


def cmd_ping(args):
    with open_conn(args.socket) as s:
        resp = send_line(s, "PING")
        print(resp)
        return 0 if resp.startswith("OK") else 1


def cmd_status(args):
    with open_conn(args.socket) as s:
        resp = send_line(s, "STATUS")
        print(resp)
        return 0 if resp.startswith("OK") else 1


def cmd_arm(args):
    with open_conn(args.socket) as s:
        resp = send_line(s, "ARM %s" % args.seconds)
        print(resp)
        return 0 if resp.startswith("OK") else 1


def cmd_disarm(args):
    with open_conn(args.socket) as s:
        resp = send_line(s, "DISARM")
        print(resp)
        return 0 if resp.startswith("OK") else 1


def cmd_heartbeat(args):
    with open_conn(args.socket) as s:
        resp = send_line(s, "HEARTBEAT")
        print(resp)
        return 0 if resp.startswith("OK") else 1


def _arm_window_foreground(args):
    """Connect, ARM, heartbeat-loop until window expires or hit-file appears,
    DISARM, return."""
    end_time = time.time() + args.seconds
    # A stale hit file from a previous debug button run must not collapse the
    # next ARM window. Hits are only meaningful after this process starts.
    # Failure modes here are subtle:
    #   - FileNotFoundError: nothing to clean — fine, proceed.
    #   - OSError (e.g. PermissionError on /tmp's sticky bit when this
    #     process can't unlink a file owned by another user): the file
    #     remains on disk. If we proceed silently, the very next loop
    #     iteration will see os.path.exists() == True and trigger an
    #     immediate EARLY_DISARM. That looks like "the user hit"
    #     when actually the unlink failed. Fail-safe-and-loud: log,
    #     audit, and refuse to ARM. Default-deny on uncertainty.
    if args.gate_file:
        try:
            os.unlink(args.gate_file)
        except FileNotFoundError:
            pass
        except OSError as e:
            if os.path.exists(args.gate_file):
                msg = ("STALE_HIT_UNLINK_FAILED file=%s err=%r — refusing "
                       "to ARM. Run as the same user that owns the file, "
                       "or remove it manually."
                       % (args.gate_file, e))
                print(msg, file=sys.stderr)
                _audit(args, "ARM_REFUSED reason=stale_hit_unlink_failed err=%r" % e)
                return 1
            # Unlink raised but the file is gone — race won, fine.
    with open_conn(args.socket) as s:
        resp = send_line(s, "ARM %s" % args.seconds)
        if not resp.startswith("OK"):
            print("ARM_FAILED %s" % resp, file=sys.stderr)
            _audit(args, "ARM_FAILED resp=%s window=%s" % (resp, args.seconds))
            return 1
        print("ARMED window=%ss gate_file=%s" % (args.seconds, args.gate_file))
        _audit(args, "GATE_ARM window=%ss" % args.seconds)
        try:
            while time.time() < end_time:
                # In normal mode, an appearing gate_file collapses the
                # window immediately (the user hit — gate's job is done).
                # In --no-early-disarm mode (debug), keep the gate armed for
                # the full window regardless so an operator can observe the
                # arm/disarm path end-to-end in logs.
                if (args.gate_file
                        and not args.no_early_disarm
                        and os.path.exists(args.gate_file)):
                    resp = send_line(s, "DISARM")
                    print("EARLY_DISARM trigger=gate_file resp=%s" % resp)
                    _audit(args, "GATE_DISARM reason=gate_file")
                    return 0
                time.sleep(args.hb_interval)
                if time.time() >= end_time:
                    break
                resp = send_line(s, "HEARTBEAT")
                if not resp.startswith("OK"):
                    print("HEARTBEAT_FAIL %s" % resp, file=sys.stderr)
                    _audit(args, "GATE_DISARM reason=heartbeat_fail resp=%s" % resp)
                    # Try to disarm anyway, then exit error.
                    try:
                        send_line(s, "DISARM")
                    except Exception:
                        pass
                    return 1
            resp = send_line(s, "DISARM")
            print("WINDOW_ENDED disarm_resp=%s" % resp)
            _audit(args, "GATE_DISARM reason=window_ended")
            return 0
        except KeyboardInterrupt:
            try:
                send_line(s, "DISARM")
            except Exception:
                pass
            _audit(args, "GATE_DISARM reason=interrupted")
            print("INTERRUPTED disarmed")
            return 130


def _daemonize():
    """Classic double-fork to detach from controlling terminal."""
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    # Redirect stdio to /dev/null so we don't hold the parent's terminal.
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        try:
            os.dup2(devnull, fd)
        except OSError:
            pass


def cmd_arm_window(args):
    if args.background:
        # Fork a child that does the work; parent returns immediately so the
        # HOOK invocation doesn't block the score-watcher.
        # NOTE: the printed pid is the intermediate child's pid. That child
        # immediately calls _daemonize() which double-forks again, so the
        # actual long-lived daemon is the GRANDCHILD with a different pid.
        # If you need to find the real daemon for monitoring/killing:
        #   ps -eo pid,ppid,cmd | grep "gate_bridge.*arm-window.*--background" | grep -v grep
        # The intermediate pid is still useful for "did the fork succeed."
        pid = os.fork()
        if pid > 0:
            print("BG_SPAWNED intermediate_pid=%d window=%ss "
                  "(real daemon detaches under init)"
                  % (pid, args.seconds))
            return 0
        _daemonize()
        # Open a dedicated log so we can audit background runs after the fact.
        log = "/tmp/gate_bridge_bg.log"
        try:
            sys.stdout = open(log, "a", buffering=1)
            sys.stderr = sys.stdout
        except OSError:
            pass
        rc = _arm_window_foreground(args)
        os._exit(rc)
    else:
        return _arm_window_foreground(args)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--socket", default="/tmp/touhou_gate.sock")
    p.add_argument("--gate-file", default="/tmp/score_hit",
                   help="early disarm if this file appears (touched by the "
                        "button daemon when the user presses the configured key)")
    p.add_argument("--hb-interval", type=float, default=0.5,
                   help="seconds between HEARTBEAT in arm-window")
    p.add_argument("--audit-file", default="/tmp/gate_audit.log",
                   help="append GATE_ARM/GATE_DISARM audit lines here.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ping").set_defaults(func=cmd_ping)
    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("disarm").set_defaults(func=cmd_disarm)
    sub.add_parser("heartbeat").set_defaults(func=cmd_heartbeat)

    a = sub.add_parser("arm")
    a.add_argument("seconds", type=float)
    a.set_defaults(func=cmd_arm)

    aw = sub.add_parser("arm-window")
    aw.add_argument("seconds", type=float)
    aw.add_argument("--background", action="store_true",
                    help="daemonize so the calling HOOK returns immediately")
    aw.add_argument("--no-early-disarm", action="store_true",
                    help="ignore --gate-file and hold the gate armed for "
                         "the full window. Debug-mode visibility for the "
                         "software gate's arm/disarm log path.")
    aw.set_defaults(func=cmd_arm_window)

    args = p.parse_args()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()

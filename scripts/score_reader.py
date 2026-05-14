#!/usr/bin/env python3
"""Standalone debug helper — print one snapshot of TH12's live runtime
state (score, stage, difficulty, frame, power, piv, graze) read directly
from the running process via /proc/<pid>/mem.

Designed for aarch64 Linux (Orange Pi 5) where the game runs under
Hangover wine + Box64. TH12 v1.00b is a 32-bit PE32 with preferred
image base 0x00400000; if the loader maps it elsewhere we offset by
(actual_base - 0x00400000) for all addresses.

Usage:
    sudo ./score_reader.py            # one snapshot
    sudo ./score_reader.py --watch    # continuous polling

Requires: same UID as game OR root, ptrace permission.
"""

import argparse
import os
import re
import struct
import sys
import time

TH12 = {
    "exe":   "th12.exe",
    "score": 0x004B0C44,
    "stage": 0x004B0CB0,
    "diff":  0x004AEBD0,
    "power": 0x004B0C48,
    "piv":   0x004B0C78,
    "graze": 0x004B0CDC,
    "frame": 0x004B0CBC,
    "score_mult": 10,
    "power_mult": 5,
}

PREFERRED_IMAGE_BASE = 0x00400000


def find_pid(exe_name):
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/comm") as f:
                if f.read().strip() == exe_name:
                    return int(pid)
        except (FileNotFoundError, PermissionError):
            continue
    return None


def find_image_base(pid, exe_name):
    try:
        with open(f"/proc/{pid}/maps") as f:
            for line in f:
                if exe_name.lower() not in line.lower():
                    continue
                m = re.match(r"([0-9a-f]+)-[0-9a-f]+ r-xp", line)
                if m:
                    return int(m.group(1), 16)
    except (FileNotFoundError, PermissionError):
        return None
    return None


def open_mem(pid):
    return open(f"/proc/{pid}/mem", "rb")


def read_dword(mem, addr):
    mem.seek(addr)
    data = mem.read(4)
    if len(data) != 4:
        return None
    return struct.unpack("<I", data)[0]


def snapshot(pid, base, game_cfg):
    out = {}
    delta = base - PREFERRED_IMAGE_BASE if base else 0
    with open_mem(pid) as mem:
        for key, addr in game_cfg.items():
            if not isinstance(addr, int):
                continue
            if key in ("score_mult", "power_mult"):
                continue
            v = read_dword(mem, addr + delta)
            out[key] = v
    score = (out.get("score") or 0) * game_cfg.get("score_mult", 1)
    power = (out.get("power") or 0) * game_cfg.get("power_mult", 1)
    out["score_display"] = score
    out["power_display"] = power
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--interval", type=float, default=0.5)
    args = ap.parse_args()

    cfg = TH12
    exe = cfg["exe"]
    pid = find_pid(exe)
    if pid is None:
        print(f"[!] {exe} not running", file=sys.stderr)
        return 2

    base = find_image_base(pid, exe)
    print(f"[+] {exe} pid={pid} image_base={hex(base) if base else '?'}", file=sys.stderr)

    if args.watch:
        last = None
        try:
            while True:
                snap = snapshot(pid, base, cfg)
                if snap != last:
                    print(
                        f"score={snap['score_display']:>12d} "
                        f"stage={snap.get('stage')} "
                        f"diff={snap.get('diff')} "
                        f"power={snap['power_display']} "
                        f"frame={snap.get('frame')} "
                        f"lframe={snap.get('lframe')}"
                    )
                    last = snap
                time.sleep(args.interval)
        except KeyboardInterrupt:
            pass
    else:
        snap = snapshot(pid, base, cfg)
        for k, v in snap.items():
            print(f"{k}={v}")


if __name__ == "__main__":
    sys.exit(main() or 0)

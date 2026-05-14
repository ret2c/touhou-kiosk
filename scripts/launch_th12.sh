#!/bin/bash
# Touhou TH12 — TH12 launcher with vertical playfield-only display + score overlay.
#
# Brings up:
#   - bare Xorg on vt1 (no DM, no compositor)
#   - TH12 in a 640x480 wine virtual desktop, rendered via Hangover wine 11.4
#   - xrandr transform that crops to the playfield region (game x 32..416,
#     y 16..464) and scales it to the full vertical 480x800 panel
#   - score_overlay.py reading TH12's score address via /proc/<pid>/mem,
#     positioned at top-center of the visible playfield
#
# Run as the `ubuntu` user. Score overlay needs root for /proc/<pid>/mem so
# the script invokes it via `sudo`.

set -euo pipefail

GAME=th12
GAME_EXE="/home/ubuntu/games/th12/th12.exe"
WINEPREFIX=/home/ubuntu/.wine_touhou           # shared D3D9 prefix
HANGOVER_WINE=/opt/touhou-hangover/usr/bin/wine
KIOSK_DIR=/home/ubuntu/touhou-kiosk
OVERLAY=${KIOSK_DIR}/score_overlay.py

# Optional /etc/default/touhou-kiosk can override the threshold and hook
# without editing this script. Useful for switching between debug (10k) and
# production (200k), or for installing an external hit hook.
THRESHOLD=10000
HOOK='echo "GATE_EVENT $(date -Iseconds)" >> /tmp/gate_audit.log'
# Hit window — how long the HIT flash stays up between threshold cross
# and stage restart. 30s for production (real hit window), 5s for
# debug (don't want to wait 30s between every test cycle).
GATE_WINDOW=5
# DEBUG mode — when set to non-empty, the launcher spawns a small
# flashing red "DEBUG" overlay in the top-left corner of the panel.
# Reminds anyone looking at the kiosk that this is not production.
DEBUG_MODE=1
# Panel backlight brightness 0..max. Written to /sys/class/backlight/*/brightness
# on every launch so the value survives kiosk restarts. Without this, the
# panel's own brightness can drift across HDMI re-init cycles.
# Empty/unset = leave alone.
PANEL_BRIGHTNESS=120
[ -r /etc/default/touhou-kiosk ] && . /etc/default/touhou-kiosk

# In debug mode, hold the gate armed for the full window even when
# /tmp/score_hit appears, so an operator can observe the gate's full
# arm window in logs. Append the flag to
# the canonical gate_bridge HOOK (only if HOOK looks like that form).
if [ -n "${DEBUG_MODE:-}" ]; then
    case "$HOOK" in
        *gate_bridge.py*arm-window*--no-early-disarm*)
            : ;;  # already present
        *gate_bridge.py*arm-window*)
            HOOK="$HOOK --no-early-disarm" ;;
    esac
fi

# Apply the panel brightness — even on restart, so it stays consistent.
if [ -n "${PANEL_BRIGHTNESS:-}" ]; then
    for bl in /sys/class/backlight/*/brightness; do
        [ -w "$bl" ] && echo "$PANEL_BRIGHTNESS" > "$bl" 2>/dev/null || \
            echo "$PANEL_BRIGHTNESS" | sudo tee "$bl" >/dev/null 2>&1
    done
fi

# Kill any stragglers from a prior session before claiming :0. Wineserver
# socket files in /tmp/.wine-* survive process death and confuse the next
# wine instance into reusing stale state — so wipe them too. Same for
# X server lock and Unix-domain socket. Also delete TH12's runtime cfg so
# the title menu always opens with "Game Start" highlighted (cursor
# position persists in this file across launches).
# Best-effort close of any stale gate window before we tear down display/game
# processes. Keep this before process cleanup so a restarted kiosk never leaves
# the software gate armed from a previous crashed watcher/bridge.
"${KIOSK_DIR}/gate_bridge.py" --socket /tmp/touhou_gate.sock disarm \
    >/dev/null 2>&1 || true

# Hard-kill stragglers from any prior game session. Do not blanket-kill
# python3: the fake/real gate MCU and the helper-button daemon are Python
# services today, and they must survive kiosk restarts. Only target the
# kiosk-owned Python helpers explicitly.
#
# Wine helper processes can hold prefix state across launches and produce
# rendering ghosts on the panel if left orphaned, so reap wineboot,
# wineserver, and the preloaders explicitly before starting a fresh
# session. `wineserver -k` (further below) ensures the wineserver socket
# closes cleanly so the new wine doesn't attach to a half-dying one.
sudo killall -9 wine wine-preloader wine64-preloader Xorg xinit \
                explorer.exe wineserver wineboot.exe winedevice.exe \
                services.exe plugplay.exe "${GAME}.exe" start.exe \
                "${GAME}_stageswitch_inject.exe" 2>/dev/null || true
# killall-by-name can't catch wine procs that have prctl-renamed themselves
# to arbitrary Windows binary names (cmd.exe, conhost.exe, ping.exe, the
# user's own .exe, etc.) and aren't in our explicit list. Robust fallback:
# kill anything whose /proc/PID/environ contains our WINEPREFIX path.
sudo python3 - "${WINEPREFIX}" <<'PY' 2>/dev/null || true
import os, signal, sys
target = ("WINEPREFIX=" + sys.argv[1]).encode()
me = os.getpid()
for name in os.listdir("/proc"):
    if not name.isdigit(): continue
    pid = int(name)
    if pid == me: continue
    try:
        if target in open(f"/proc/{pid}/environ", "rb").read():
            os.kill(pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
PY
# Send wineserver its own kill signal in case the killall missed a copy
# under a different exec name. -k tears down the prefix's server state.
sudo -u ubuntu env WINEPREFIX="${WINEPREFIX}" \
    "${HANGOVER_WINE%/wine}/wineserver" -k 2>/dev/null || true
# Brief settle so the kernel reaps zombies before we spawn replacements.
sleep 1

# Ensure audit logs are writable. /tmp/gate_audit.log and /tmp/overlay.log
# can end up locked/un-writable across kiosk restarts (observed
# even root got Permission denied appending to /tmp/gate_audit.log after
# extended uptime — likely a stale fd holding the inode open in a way that
# blocks new writers). Recreate with 666 so any process can append.
for f in /tmp/gate_audit.log /tmp/overlay.log /tmp/watcher.log; do
    if [ -e "$f" ] && ! { : >> "$f"; } 2>/dev/null; then
        sudo rm -f "$f"
    fi
    [ -e "$f" ] || sudo touch "$f"
    sudo chmod 666 "$f" 2>/dev/null || true
done
kill_python_helper() {
    local script="$1"
    sudo python3 - "$script" <<'PY'
import os
import signal
import sys

target = os.path.realpath(sys.argv[1])
me = os.getpid()

for name in os.listdir("/proc"):
    if not name.isdigit():
        continue
    pid = int(name)
    if pid == me:
        continue
    try:
        raw = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
    except OSError:
        continue
    argv = [x.decode("utf-8", "replace") for x in raw if x]
    if len(argv) < 2:
        continue
    try:
        candidate = os.path.realpath(argv[1])
    except OSError:
        candidate = argv[1]
    if candidate == target:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
PY
}
kill_python_helper "$OVERLAY"
kill_python_helper "${KIOSK_DIR}/score_watcher.py"
kill_python_helper "${KIOSK_DIR}/debug_overlay.py"
kill_python_helper "${KIOSK_DIR}/debug_log_overlay.py"
sleep 3
sudo rm -f /tmp/.X*-lock
sudo rm -rf /tmp/.X11-unix/X*
sudo rm -rf /tmp/.wine-*
GAME_DIR=$(dirname "${GAME_EXE}")
rm -f "${GAME_DIR}/${GAME}.cfg"
# scoreth12.dat carries the title-menu cursor position. Wine restarts that
# do NOT regenerate this file unless we delete it; without deletion the
# cursor stays on whatever menu item the previous game session ended on
# (commonly "Practice Start") and the auto-advance Z lands on the wrong
# branch. The .dat is just the high-score table — losing it costs nothing
# in a kiosk context.
rm -f "${GAME_DIR}/score${GAME}.dat"

# A kiosk restart means a fresh game session. Clear any stale button-daemon
# logical state from the previous process tree; if this file is left as
# `sleep`, score_watcher intentionally suppresses stuck detection forever.
# It is normally root-owned because button_daemon runs as root.
printf 'playing\n' | sudo tee /tmp/touhou_state >/dev/null || true

cat >/tmp/run_${GAME}_hit.sh <<RUNEOF
#!/bin/bash
exec >> /tmp/${GAME}.log 2>&1
echo "=== ${GAME} hit \$(date -Iseconds) ==="
export DISPLAY=:0
export WINEPREFIX=${WINEPREFIX}
export HOME=/home/ubuntu
export WINEDLLOVERRIDES="mscoree=d;mshtml=d;winevulkan=d"
export WINEDEBUG=-all
export PATH=/opt/touhou-hangover/usr/bin:\$PATH

# Disable all blanking — hit display must never sleep.
xset s off s noblank s 0 0 2>/dev/null
xset -dpms 2>/dev/null
xset dpms 0 0 0 2>/dev/null

# Rotate to landscape (800x480 logical) BEFORE wine starts so wine reads
# RANDR primary as wider than its 640x480 virt desktop request and creates
# the desktop at the requested 640x480 instead of clamping.
xrandr --output HDMI-1 --rotate left 2>/dev/null

cd "\$(dirname "${GAME_EXE}")"
exec ${HANGOVER_WINE} explorer /desktop=${GAME},640x480 "${GAME_EXE}"
RUNEOF
chmod +x /tmp/run_${GAME}_hit.sh

cat >/tmp/xinit_${GAME}_hit <<XINEOF
#!/bin/sh
exec /tmp/run_${GAME}_hit.sh
XINEOF
chmod +x /tmp/xinit_${GAME}_hit

rm -f /tmp/${GAME}.log

# Start xinit (Xorg + wine + game) in the background but track its PID — we
# wait on it at the end so this script keeps running for systemd's Type=simple.
xinit /tmp/xinit_${GAME}_hit -- :0 vt1 -nolisten tcp \
    >/tmp/xinit_stdout.log 2>&1 &
XINIT_PID=$!

# Auto-advance + overlay run as a detached subshell so they don't block the
# wait below.
(
    export DISPLAY=:0

    # ---- helpers ---------------------------------------------------------

    # Find the live th12.exe pid (skip zombies / cmdline-matchers).
    game_pid() {
        # Match by comm (not -x, since the threads have the same
        # comm), require readable maps (rules out kernel-side
        # dying processes), and verify State != Z so we don't
        # return a zombie that satisfies the other checks but
        # can't actually be ptraced or memory-read.
        for p in $(pgrep th12.exe 2>/dev/null); do
            [ "$(cat /proc/$p/comm 2>/dev/null)" = "th12.exe" ] \
                && [ -r /proc/$p/maps ] \
                && [ "$(awk '/^State:/{print $2}' /proc/$p/status 2>/dev/null)" != "Z" ] \
                && { echo "$p"; return 0; }
        done
        return 1
    }

    # Read TH12's stage memory address. Echoes the integer or empty.
    read_stage() {
        local pid; pid=$(game_pid) || return 1
        sudo python3 -c "
import struct, sys
try:
    with open('/proc/${pid}/mem','rb',buffering=0) as f:
        f.seek(0x004B0CB0); print(struct.unpack('<I',f.read(4))[0])
except Exception: sys.exit(1)
" 2>/dev/null
    }

    # Read stage + frame + lives in one /proc/mem open. Echoes
    # "stage frame lives" (space-separated) or empty on failure. Used
    # by the menu-nav success check to distinguish a real "in stage 1
    # actively playing" state from a transient stage=1 flicker during
    # menu transitions.
    read_stage_frame_lives() {
        local pid; pid=$(game_pid) || return 1
        sudo python3 -c "
import struct, sys
try:
    with open('/proc/${pid}/mem','rb',buffering=0) as f:
        f.seek(0x004B0CB0); stage = struct.unpack('<I', f.read(4))[0]
        f.seek(0x004B0CBC); frame = struct.unpack('<I', f.read(4))[0]
        f.seek(0x004B0CA0); lives = struct.unpack('<I', f.read(4))[0]
        print('%d %d %d' % (stage, frame, lives))
except Exception: sys.exit(1)
" 2>/dev/null
    }

    # Find the ZUN startup dialog (mojibake "?????" title) — returns wid.
    find_zun_dialog() {
        xdotool search "" 2>/dev/null | while read w; do
            n=$(xdotool getwindowname $w 2>/dev/null) || continue
            [ -n "$n" ] \
                && [ "$n" != "Default IME" ] \
                && [ "$n" != "${GAME} - Wine Desktop" ] \
                && [ -z "${n##\?\?\?\?\?*}" ] \
                && echo "$w" \
                && return 0
        done
    }

    # Find the main game window (title contains "Undefined"). Returns wid.
    find_game_window() {
        xdotool search --name "Undefined" 2>/dev/null | head -1
    }

    # Send keys to the game with primed focus. \$1 is space-separated keys.
    send_keys() {
        local gw; gw=$(find_game_window)
        [ -z "$gw" ] && return 1
        xdotool windowactivate --sync "$gw" 2>/dev/null || true
        sleep 0.3
        # shellcheck disable=SC2086
        xdotool key --delay 400 $1
    }

    # Single attempt at navigating Title → stage 1 Normal Reimu Type A.
    # On TH12 the title menu always opens with "Game Start" highlighted on
    # cold boot, so Z confirms it directly. Down switches Easy→Normal at
    # Rank Select. Final 5x repeat-Z burst advances Player→Shot→stage 1.
    # If a previous run left the cursor on a different menu option, the
    # verify-and-retry loop in the caller will catch the mis-fire and
    # re-run this — but this function intentionally does NOT try to "force
    # to top" with Esc/Ups because Esc on title screen opens a Quit-confirm
    # dialog and Up arrows can move the cursor to wrong items mid-modal.
    do_menu_nav() {
        echo "[+] menu_nav: Z + Down + 5x Z, single xdotool call" >&2
        local gw; gw=$(find_game_window)
        if [ -z "$gw" ]; then
            echo "[!] no game window found" >&2
            return 1
        fi
        xdotool windowactivate --sync "$gw" 2>/dev/null || true
        sleep 0.5
        # All keys in a SINGLE xdotool call. Wine's keyboard grab can
        # shift between separate xdotool processes, dropping events.
        # Sequence assumes the title-menu cursor defaults to Game Start
        # — guaranteed when th12.cfg + scoreth12.dat were deleted in the
        # outer launcher cleanup.
        #   z        — confirm Game Start -> Rank Select
        #   Down     — Easy -> Normal
        #   6x z     — Normal -> Player Select (Reimu) -> Weapon Select
        #              -> Type A -> stage 1 begins (last 2 z's are
        #              harmless shots in stage; TH12 weapon select
        #              needs more confirmation than a 5-Z burst).
        xdotool key --delay 250 z Down z z z z z z
        sleep 1
    }

    # ---- wait for game to reach the ZUN dialog ---------------------------

    echo "[+] auto-advance: waiting for ZUN startup dialog" >&2
    DIALOG=""
    for i in $(seq 1 60); do
        sleep 1
        DIALOG=$(find_zun_dialog || true)
        [ -n "${DIALOG:-}" ] && { echo "[+] dialog up after ${i}s" >&2; break; }
    done

    if [ -n "${DIALOG:-}" ]; then
        xdotool windowfocus "$DIALOG" 2>/dev/null
        xdotool key --window "$DIALOG" Return 2>/dev/null || true
    else
        echo "[!] no ZUN dialog appeared after 60s; trying anyway" >&2
    fi
    sleep 6

    # ---- apply playfield-crop xrandr transform --------------------------
    # Wine's 640x480 virt desktop is locked at this point; the transform
    # changes how the panel displays it without resizing the wine window.
    xrandr --fb 640x480 --output HDMI-1 --rotate normal \
           --transform 0.8,0,32,0,0.56,16,0,0,1 2>/dev/null || true

    # ---- software brightness (panel is physically very bright) ----------
    # xrandr --brightness is a software multiplier on the framebuffer
    # before output. NOT the kernel backlight (PANEL_BRIGHTNESS handles
    # that). This is the "the panel max-out is still too bright even
    # at low backlight" knob. Applies after the transform so the
    # multiplier covers the visible playfield region.
    if [ -n "${XRANDR_BRIGHTNESS:-}" ]; then
        xrandr --output HDMI-1 --brightness "$XRANDR_BRIGHTNESS" 2>/dev/null || true
    fi

    # ---- hide the X11 default cursor ------------------------------------
    # xinit with no WM leaves the default X cursor (chunky white-outlined
    # "X" crosshair) visible wherever the pointer last sat — very
    # noticeable on the bottom of the screen during testing.
    #   1) xsetroot empty cursor on the root window (covers cases where
    #      pointer is over root)
    #   2) cursor="none" in each Tk overlay's configure() (covers tk
    #      child windows — without it Tk inherits the X default, not the
    #      root cursor, so xsetroot alone wasn't enough)
    #   3) park the pointer at fb(36, 24) which is inside the DEBUG flash
    #      overlay (top-left, also has cursor="none") so even if Wine's
    #      virtual desktop pulls focus, the cursor lands on a hidden-cursor
    #      window.
    # No `unclutter` available in apt on this image.
    cat >/tmp/empty.xbm <<'XBMEOF'
#define empty_width 1
#define empty_height 1
static unsigned char empty_bits[] = { 0x00 };
XBMEOF
    xsetroot -cursor /tmp/empty.xbm /tmp/empty.xbm 2>/dev/null || true
    # X root window solid black so the gap between service stop and
    # respawn (when score_overlay's mask is briefly gone) doesn't expose
    # the default gray X background. Persists across kiosk restarts:
    # the root pixel value is set on the X server, not in our process.
    xsetroot -solid '#000000' 2>/dev/null || true
    xdotool mousemove 36 24 2>/dev/null || true

    # ---- start overlay early --------------------------------------------
    # Score overlay starts BEFORE menu_nav so by the time stage 1 is
    # actually playing the overlay is already on top of the game,
    # showing live state. While menu_nav is running, the overlay
    # displays "playfield not loaded" (its built-in stage==0 fallback)
    # which is fine. Starting post-menu_nav left a visible 5-10s gap
    # where stage 1 was up but no score display.
    sudo "$OVERLAY" "$GAME" >/tmp/overlay.log 2>&1 &
    sleep 1
    TK=$(xdotool search --name "^tk$" | head -1)
    [ -n "$TK" ] && xdotool windowraise "$TK"

    # ---- DEBUG-mode indicators -----------------------------------------
    # In debug mode (threshold=10k, hit window=5s, etc.), spawn two
    # overlays:
    #   - debug_overlay.py: small flashing red "DEBUG" mark, top-left
    #   - debug_log_overlay.py: last 5 lines of /tmp/watcher.log,
    #     bottom-left (NOT bottom-right; items spawn there)
    # Both make it impossible to mistake the running kiosk for
    # production state.
    if [ -n "${DEBUG_MODE:-}" ]; then
        sudo "${KIOSK_DIR}/debug_overlay.py" >/tmp/debug_overlay.log 2>&1 &
        sudo "${KIOSK_DIR}/debug_log_overlay.py" >/tmp/debug_log_overlay.log 2>&1 &
        echo "[+] DEBUG_MODE on; spawned debug_overlay + debug_log_overlay" >&2
        sleep 0.5
        # Raise all tk windows so all overlays sit above the wine game
        # window.
        for TK in $(xdotool search --name "^tk$"); do
            xdotool windowraise "$TK"
        done
    fi

    # ---- menu nav with verify-and-retry ---------------------------------
    #
    # Success check requires THREE conditions, not just stage==1. A
    # stage memory read of "1" can show up as
    # a transient flicker during menu transitions; lives>0 + an
    # advancing frame counter confirm we're actually playing, not
    # peeking at a moment between menu clicks.
    SUCCESS=0
    # When COLD_BOOT_RANDOM_STAGE=1, fire stage-switch the moment
    # stage==1 + lives>0 is detected (any frame >= 1), rather than
    # waiting for a later frame window. This hides stage 1 — the player
    # sees menu_nav flash, then the chosen random stage's title card
    # directly.
    for attempt in 1 2 3; do
        echo "[+] menu_nav attempt ${attempt}/3" >&2
        do_menu_nav
        for w_ms in $(seq 1 150); do
            SFL=$(read_stage_frame_lives || echo "")
            if [ -n "$SFL" ]; then
                read s f l <<< "$SFL"

                # ---- cold-boot random-stage early-fire ----
                # As soon as stage 1 is alive (any frame >= 1, lives > 0),
                # invoke stage-switch with random target 1..6.
                if [ -n "${COLD_BOOT_RANDOM_STAGE:-}" ] \
                        && [ "$s" = "1" ] && [ "$l" -gt 0 ] 2>/dev/null \
                        && [ "$f" -ge 1 ] 2>/dev/null; then
                    SS_INJECT_EARLY="/home/ubuntu/games/th12/th12_stageswitch_inject.exe"
                    if [ -x "$SS_INJECT_EARLY" ]; then
                        TARGET_EARLY=$(awk 'BEGIN{srand(); print int(1+rand()*6)}')
                        [ "$TARGET_EARLY" -lt 1 ] && TARGET_EARLY=1
                        [ "$TARGET_EARLY" -gt 6 ] && TARGET_EARLY=6
                        echo "[+] early stage-switch: stage=1 frame=${f} -> target=${TARGET_EARLY}" >&2
                        sudo -u ubuntu env DISPLAY=:0 \
                            WINEPREFIX=/home/ubuntu/.wine_touhou \
                            HOME=/home/ubuntu WINEDEBUG=-all \
                            PATH=/opt/touhou-hangover/usr/bin:/usr/bin:/bin \
                            /opt/touhou-hangover/usr/bin/wine \
                            "$SS_INJECT_EARLY" --stage "$TARGET_EARLY" \
                            --dll 'Z:\home\ubuntu\games\th12\th12_stageswitch_v4.dll' \
                            >> /tmp/watcher.log 2>&1 \
                            || echo "[!] early stage-switch failed; staying on stage 1" >&2
                        SUCCESS=1
                        # If the early stage-switch failed and we end up
                        # stuck on stage 1, the watcher's reset ladder
                        # will still try stage_switch on first trigger,
                        # then fall to native_restart, then systemctl.
                        break 2
                    fi
                fi

                # ---- Original path (no COLD_BOOT_RANDOM_STAGE) ------
                # Poll up to 15s for stage==1 AND lives>0 AND frame in
                # target range [110, 250]. The frame target is the
                # title-card-visible window — capture here so post-
                # restore visual shows "stage starting" not "mid-stage
                # with enemies".
                if [ "$s" = "1" ] && [ "$l" -gt 0 ] 2>/dev/null \
                   && [ "$f" -ge 110 ] && [ "$f" -le 250 ] 2>/dev/null; then
                    echo "[+] confirmed stage=1 lives=${l} frame=${f} (in [110,250] target) attempt ${attempt}" >&2
                    SUCCESS=1
                    break 2
                fi
                # Past the target window: stage 1 confirmed but we
                # missed the early-frame target. Accept current frame
                # anyway so the kiosk isn't stuck forever.
                if [ "$s" = "1" ] && [ "$l" -gt 0 ] 2>/dev/null \
                   && [ "$f" -gt 250 ] 2>/dev/null; then
                    echo "[!] missed [110,250] window — accepting stage=1 at frame=${f}" >&2
                    SUCCESS=1
                    break 2
                fi
            fi
            sleep 0.1
        done
        echo "[!] attempt ${attempt} did not confirm playable stage 1 (last SFL=${SFL:-?})" >&2
    done

    if [ "$SUCCESS" != "1" ]; then
        echo "[!] menu_nav failed 3 times — restarting service" >&2
        sudo systemctl restart touhou-kiosk.service
        exit 0
    fi

    # ---- start watcher --------------------------------------------------
    # (Overlay started earlier, pre-menu_nav.)
    # Re-raise the overlay just in case wine repainting put it behind
    # the game window during stage transition.
    TK=$(xdotool search --name "^tk$" | head -1)
    [ -n "$TK" ] && xdotool windowraise "$TK"

    # Score-gate watcher: threshold + hook come from /etc/default/touhou-kiosk
    # if present, else the defaults at the top of this script (10k debug
    # threshold + echo to /tmp/gate_audit.log).
    # --state-file makes the watcher's stuck-detection skip when the button
    # daemon has put the kiosk into "sleep" (frame counter freezes by design
    # during sleep; without this guard the watcher would silent-restart the
    # kiosk after 10s of paused play).
    #
    # Trim /tmp/watcher.log to last 200 KB on each watcher launch so it
    # doesn't grow unbounded (score= lines fire every score change ≈
    # several Hz; ~20 KB/min during active play). Keep the tail so the
    # last cycle's diagnostics are preserved.
    if [ -f /tmp/watcher.log ] \
            && [ "$(stat -c%s /tmp/watcher.log 2>/dev/null || echo 0)" -gt 204800 ] 2>/dev/null; then
        tail -c 204800 /tmp/watcher.log > /tmp/watcher.log.trimmed \
            && mv /tmp/watcher.log.trimmed /tmp/watcher.log \
            && echo "[+] trimmed /tmp/watcher.log to last 200KB" \
                >> /tmp/watcher_supervisor.log
    fi

    # Wrapped in a supervision loop. The launcher waits on xinit, not on
    # the watcher, so a silent watcher death would leave the service
    # "active" with no score detection. Supervision respawns the watcher
    # within seconds and logs each exit so it can't go unnoticed.
    # Tiered back-off prevents tight respawn loops when the watcher is
    # crashing on every start (e.g. th12 went away).
    #
    # WATCHER_DISABLE — testing escape hatch. When set in
    # /etc/default/touhou-kiosk (or the systemd unit) the watcher
    # supervisor doesn't start; the game runs without threshold
    # detection or stuck recovery. Useful for development testing
    # where the watcher's Esc-on-stuck would otherwise interact with
    # the test. Production must NEVER set this.
    if [ -n "${WATCHER_DISABLE:-}" ]; then
        echo "[+] WATCHER_DISABLE set; skipping watcher supervisor (test mode)" \
            >> /tmp/watcher_supervisor.log
    else
    (
        # The launcher uses `set -euo pipefail`; that's a problem inside a
        # respawn loop because the watched command will frequently exit
        # non-zero (SIGTERM → 143, etc.) and we MUST keep looping. Disable
        # both -e and pipefail inside this subshell so non-zero rc is just
        # a number, not a fatal abort.
        set +e
        set +o pipefail
        attempts=0
        th12_gone_streak=0   # consecutive watcher exits with rc=2 (th12 not found)
        th12_gone_first=0    # epoch seconds of first rc=2 in current streak
        while true; do
            attempts=$((attempts + 1))
            echo "[+] watcher_supervisor: attempt=$attempts at $(date -Iseconds)" \
                >> /tmp/watcher_supervisor.log
            # Trim watcher.log on EVERY attempt.
            if [ -f /tmp/watcher.log ] \
                    && [ "$(stat -c%s /tmp/watcher.log 2>/dev/null || echo 0)" -gt 204800 ] 2>/dev/null; then
                tail -c 204800 /tmp/watcher.log > /tmp/watcher.log.trimmed \
                    && mv /tmp/watcher.log.trimmed /tmp/watcher.log \
                    && echo "[+] trimmed watcher.log to last 200KB" \
                        >> /tmp/watcher_supervisor.log
            fi
            sudo "${KIOSK_DIR}/score_watcher.py" "$GAME" \
                --threshold "$THRESHOLD" \
                --hook "$HOOK" \
                --gate-window "$GATE_WINDOW" \
                ${TOUHOU_NO_FLASH:+--no-flash} \
                --display :0 \
                --state-file /tmp/touhou_state \
                ${NATIVE_CONTINUE_DIAG:+--native-continue-diag} \
                ${TH12_NATIVE_RESTART:+--native-restart} \
                ${TH12_STAGESWITCH:+--stageswitch} \
                ${TH12_STAGESWITCH_STAGES:+--stageswitch-stages "$TH12_STAGESWITCH_STAGES"} \
                ${TOUHOU_THRESHOLD_ONLY:+--threshold-only} \
                ${DEBUG_MODE:+--no-lock} \
                ${DEBUG_MODE:+--no-early-disarm} \
                >>/tmp/watcher.log 2>&1
            rc=$?
            echo "[!] watcher_supervisor: watcher exited rc=$rc at $(date -Iseconds), attempt=$attempts" \
                >> /tmp/watcher_supervisor.log
            # th12-gone escalation: rc=2 means watcher couldn't find
            # th12.exe at startup. Without this escalation, the
            # supervisor respawns forever while th12 stays dead,
            # because the launcher waits on xinit (which keeps the
            # wine virtual desktop open even when th12 died) and
            # systemd never sees a service exit. After 3 consecutive
            # rc=2 exits within 30 s, trigger a kiosk service
            # restart to force a cold boot.
            now_epoch=$(date +%s)
            if [ "$rc" = "2" ]; then
                if [ "$th12_gone_streak" -eq 0 ]; then
                    th12_gone_first=$now_epoch
                fi
                th12_gone_streak=$((th12_gone_streak + 1))
                age=$((now_epoch - th12_gone_first))
                echo "[!] watcher_supervisor: th12-gone streak=$th12_gone_streak age=${age}s" \
                    >> /tmp/watcher_supervisor.log
                if [ "$th12_gone_streak" -ge 3 ] && [ "$age" -le 30 ]; then
                    echo "[!] watcher_supervisor: th12 gone for 3 consecutive starts within ${age}s — escalating to systemctl restart" \
                        >> /tmp/watcher_supervisor.log
                    sudo systemctl restart touhou-kiosk.service &
                    exit 0
                fi
            else
                th12_gone_streak=0
            fi
            # Back-off: 2s for first 5 attempts, then 10s, then 30s.
            if [ "$attempts" -le 5 ]; then
                sleep 2
            elif [ "$attempts" -le 20 ]; then
                sleep 10
            else
                sleep 30
            fi
        done
    ) &
    fi  # end WATCHER_DISABLE check
) &

# Block on xinit so systemd sees the launcher as alive while the game runs.
# When xinit exits (game closed, service stopped, etc.) this wait returns
# and the systemd service either restarts (Restart=on-failure) or stops.
wait "$XINIT_PID"

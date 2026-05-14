#!/usr/bin/env bash
#
# gui_run.sh — run a TH12-stack test program with the window visible on
# your Mac via XQuartz. Stack: armhf Debian 12 + Wine 8.0 i386 (with
# Apple Silicon Docker's transparent qemu-i386 binfmt). Run gui_setup.sh
# first.
#
# Usage:
#   gui_run.sh init           # 5-second smoke window
#   gui_run.sh render         # animated quad, default 1800 frames
#   gui_run.sh render 600     # explicit frame count
#   gui_run.sh dinput         # DInput logic test (no window)
#   gui_run.sh shell          # interactive shell

set -euo pipefail
CONTAINER=th12_gui

note() { printf "\033[1;36m[gui_run]\033[0m %s\n" "$*"; }
fail() { printf "\033[1;31m[ fail  ]\033[0m %s\n" "$*" >&2; exit 1; }

docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$" \
  || fail "container '$CONTAINER' not running. Run ./gui_setup.sh first."

PATH=/opt/X11/bin:$PATH xhost +127.0.0.1 >/dev/null 2>&1 || true

WINEENV=( -e DISPLAY=host.docker.internal:0 \
          -e "WINEDLLOVERRIDES=winemenubuilder.exe=d;mscoree=d;mshtml=d" \
          -e WINEPREFIX=/root/.wine )

cmd="${1:-render}"

case "$cmd" in
  init)
    note "running d3d9_init.exe — opens a slate-blue window for ~5s"
    docker exec "${WINEENV[@]}" -e WINEDEBUG=-all "$CONTAINER" \
      /usr/bin/wine /workspace/d3d9_init.exe
    ;;

  render)
    frames="${2:-1800}"
    note "running d3d9_render.exe for $frames frames"
    note "you should see a 640x480 window with a colour-cycling background"
    note "and a red quad sliding left-to-right"
    docker exec "${WINEENV[@]}" -e WINEDEBUG=-all "$CONTAINER" \
      /usr/bin/wine /workspace/d3d9_render.exe "$frames"
    ;;

  fps)
    note "600-frame benchmark (FPS will be modest — qemu-i386 path)"
    docker exec "${WINEENV[@]}" -e WINEDEBUG=-all "$CONTAINER" \
      bash -c 'time /usr/bin/wine /workspace/d3d9_render.exe 600'
    ;;

  dinput)
    note "running d3d9_dinput.exe (logic only, no window)"
    docker exec "${WINEENV[@]}" -e WINEDEBUG=-all "$CONTAINER" \
      /usr/bin/wine /workspace/d3d9_dinput.exe
    docker exec "$CONTAINER" cat /root/.wine/drive_c/users/Public/Z*/d3d9_dinput.log 2>/dev/null \
      || docker exec "$CONTAINER" cat /tmp/d3d9_dinput.log 2>/dev/null \
      || note "(logfile not found — check WINE prefix Z:/tmp/)"
    ;;

  shell)
    note "interactive shell — test programs at /workspace/*.exe"
    note "run with: /usr/bin/wine /workspace/d3d9_render.exe"
    docker exec -it "${WINEENV[@]}" "$CONTAINER" bash
    ;;

  *)
    cat <<EOF
gui_run.sh — usage:
  gui_run.sh init          5-second smoke window
  gui_run.sh render [N]    animated quad (default 1800 frames)
  gui_run.sh fps           600-frame benchmark
  gui_run.sh dinput        DInput logic test
  gui_run.sh shell         interactive shell
EOF
    exit 2
    ;;
esac

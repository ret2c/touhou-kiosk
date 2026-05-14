#!/usr/bin/env bash
#
# gui_setup.sh — set up a Docker + XQuartz environment for visible TH12-stack
# testing. Goal: pixels on your screen. Performance is not the priority here
# (the headless run.sh harness in this directory is the FPS-optimized path).
#
# Stack: linux/arm/v7 Debian 12 + Wine 8.0 i386 + mingw + Mesa.
# On Apple Silicon Docker, the linuxkit VM transparently emulates i386 via
# qemu-user-static when running 32-bit Windows PE binaries. Slower than FEX
# but Just Works™ with no rootfs fetch.
#
# Usage:
#   ./gui_setup.sh             # idempotent set-up
#   ./gui_setup.sh --rebuild   # force from scratch
#
# After setup, use ./gui_run.sh

set -euo pipefail

CONTAINER=th12_gui
IMAGE=debian:12
PLATFORM=linux/arm/v7
HERE=$(cd "$(dirname "$0")" && pwd)

note() { printf "\033[1;36m[gui_setup]\033[0m %s\n" "$*"; }
ok()   { printf "\033[1;32m[ ok    ]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[ warn  ]\033[0m %s\n" "$*"; }
fail() { printf "\033[1;31m[ fail  ]\033[0m %s\n" "$*" >&2; exit 1; }

[[ "$OSTYPE" == "darwin"* ]] || fail "this script targets macOS hosts"

if [[ "${1:-}" == "--rebuild" ]]; then
  note "removing existing container"
  docker rm -f "$CONTAINER" 2>/dev/null || true
fi

# --- 1. XQuartz check -------------------------------------------------------
note "checking XQuartz"
if ! pgrep -f Xquartz >/dev/null 2>&1; then
  note "starting XQuartz..."
  open -a XQuartz; sleep 3
fi
pgrep -f Xquartz >/dev/null 2>&1 || fail "could not start XQuartz"

if ! nc -z -w 1 localhost 6000 >/dev/null 2>&1; then
  warn "XQuartz isn't listening on TCP port 6000."
  warn "  XQuartz Preferences → Security → 'Allow connections from network clients'"
  warn "  must be ON. Then restart XQuartz and re-run this script."
  warn "  Or: defaults write org.xquartz.X11 nolisten_tcp 0 ; killall Xquartz ; open -a XQuartz"
  fail "XQuartz TCP listener not detected"
fi
ok "XQuartz running, listening on :0"

# --- 2. xhost ---------------------------------------------------------------
PATH=/opt/X11/bin:$PATH xhost +127.0.0.1 >/dev/null
PATH=/opt/X11/bin:$PATH xhost +localhost >/dev/null
ok "xhost authorized 127.0.0.1, localhost"

# --- 3. Container -----------------------------------------------------------
if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
  if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
    ok "container '$CONTAINER' already running"
  else
    docker start "$CONTAINER" >/dev/null
    ok "container '$CONTAINER' started"
  fi
else
  note "creating container '$CONTAINER' on $PLATFORM (~5 min first run)"
  docker run -d \
    --platform="$PLATFORM" \
    --name "$CONTAINER" \
    -e DISPLAY=host.docker.internal:0 \
    -v "$HERE":/workspace \
    -w /workspace \
    "$IMAGE" sleep infinity >/dev/null
  ok "container created"
fi

# --- 4. Install ------------------------------------------------------------
note "installing Wine 8.0 + mingw + X11 libs"
docker exec "$CONTAINER" bash -c '
  set -e
  if [ -f /tmp/.gui_setup_done ]; then
    echo "[container] already provisioned, skipping apt"
    exit 0
  fi
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq

  apt-get install -y --no-install-recommends \
      ca-certificates curl file procps netcat-openbsd \
      gcc-mingw-w64-i686-win32 \
      wine wine32 \
      x11-apps mesa-utils \
      libfreetype6 libpng16-16 libfontconfig1 libcups2 \
      libgl1 libgl1-mesa-dri libsdl2-2.0-0 \
      libxcomposite1 libxinerama1 libxrandr2 libxcursor1 libxi6 \
      libgnutls30 libexpat1 \
      2>&1 | tail -3

  # Make sure /usr/bin/wine exists (debian 12 wine package usually creates it)
  if [ ! -e /usr/bin/wine ] && [ -e /usr/lib/wine/wine ]; then
    ln -s /usr/lib/wine/wine /usr/bin/wine
  fi

  echo "[container] provisioning complete"
  echo "[container] wine: $(/usr/bin/wine --version 2>&1 || echo MISSING)"
  touch /tmp/.gui_setup_done
'
ok "packages installed"

# --- 5. Compile test programs ----------------------------------------------
note "compiling D3D9 / DInput8 test programs"
docker exec "$CONTAINER" bash -c '
  set -e
  cd /workspace
  for src in d3d9_init.c d3d9_render.c d3d9_dinput.c; do
    out="${src%.c}.exe"
    if [ ! -f "$out" ] || [ "$src" -nt "$out" ]; then
      echo "  compile: $src"
      i686-w64-mingw32-gcc -O2 -Wall -o "$out" "$src" \
          -ld3d9 -ldinput8 -ldxguid -lole32 -luser32 -lgdi32 \
          2>&1 | head -10
    else
      echo "  cached:  $out"
    fi
  done
'
ok "test programs compiled"

# --- 6. Wine prefix warmup -------------------------------------------------
note "initializing Wine prefix (this may take ~30 s the first time)"
docker exec "$CONTAINER" bash -c '
  if [ -f /root/.wine/.warm ]; then
    echo "[container] wine prefix already warm"; exit 0
  fi
  export WINEDLLOVERRIDES="winemenubuilder.exe=d;mscoree=d;mshtml=d"
  export WINEDEBUG=-all
  export DISPLAY=host.docker.internal:0
  timeout 60 /usr/bin/wine wineboot --init 2>&1 | tail -5 || true
  mkdir -p /root/.wine
  touch /root/.wine/.warm
'
ok "Wine prefix ready"

cat <<EOF

$(printf "\033[1;32m[ ok    ]\033[0m") GUI setup complete.

Run a visible test with:
   $HERE/gui_run.sh init       # 5-second smoke test, slate-blue window
   $HERE/gui_run.sh render     # animated quad, 30s @ 60fps target
   $HERE/gui_run.sh dinput     # logic-only DInput keyboard test
   $HERE/gui_run.sh shell      # interactive shell in the container

Note: this stack uses Apple Silicon Docker's transparent qemu-i386 emulation,
which is **slower** than the production Box64 runtime on the OPi. For
visible-pixels-on-XQuartz the simpler stack wins; for headless benchmarks
use run.sh in this directory.

EOF

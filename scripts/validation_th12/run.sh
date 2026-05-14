#!/usr/bin/env bash
# Mac-side container harness for the D3D9 / DInput8 validation binaries.
# Uses box86 + wine32 inside Docker to run the i686 PE32 smoke tests
# (d3d9_init.exe, d3d9_render.exe, d3d9_dinput.exe) without needing an
# Orange Pi 5 on the bench.
#
# This is NOT the production runtime — the OPi 5 deployment uses Hangover
# Wine 11.4 + Box64 (loaded as wowbox64.dll via wine's new-wow64 loader),
# not box86 + wine32. These container scripts exist purely for regression
# testing the validation binaries themselves on any aarch64 host.
#
# Two flavours:
#   ./run.sh armhf   - canonical box86 environment (Debian 12 armhf docker)
#   ./run.sh arm64   - production-analog stack (arm64 host + box86 + multiarch)
#   ./run.sh fex     - alternative AArch64-native stack (FEX-Emu — closer
#                       to current production path but still container-based)
#
# Each flavour:
#   1. Spawns a debian:12 container of the right architecture.
#   2. Installs box86 (or FEX), wine32:i386, mingw, Xvfb, mesa.
#   3. Cross-compiles the three test PE32 binaries from the .c files
#      next to this script.
#   4. Runs them and prints the pass/fail / FPS lines.

set -euo pipefail
cd "$(dirname "$0")"

flavour="${1:-armhf}"
case "$flavour" in
    armhf)  PLATFORM="linux/arm/v7" ;;
    arm64)  PLATFORM="linux/arm64"  ;;
    fex)    PLATFORM="linux/arm64"  ;;
    *) echo "usage: $0 {armhf|arm64|fex}" >&2; exit 1 ;;
esac

NAME="th12_repro_${flavour}_$$"
trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true' EXIT

docker run --platform="$PLATFORM" -d --name "$NAME" debian:12 sleep infinity >/dev/null

docker cp d3d9_init.c   "$NAME":/tmp/
docker cp d3d9_render.c "$NAME":/tmp/
docker cp d3d9_dinput.c "$NAME":/tmp/

docker exec "$NAME" bash -se <<EOF
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update >/dev/null
apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg2 file procps mesa-utils xvfb \
    gcc-mingw-w64-i686-win32 \
    >/dev/null

case "$flavour" in
armhf)
    curl -fsSL https://ryanfortner.github.io/box86-debs/box86.list \
        -o /etc/apt/sources.list.d/box86.list
    curl -fsSL https://ryanfortner.github.io/box86-debs/KEY.gpg \
        | gpg --dearmor -o /etc/apt/trusted.gpg.d/box86-archive-keyring.gpg
    apt-get update >/dev/null
    apt-get install -y --no-install-recommends box86-generic-arm >/dev/null

    dpkg --add-architecture i386
    apt-get update >/dev/null
    apt-get install -y --no-install-recommends \
        wine32:i386 \
        libfreetype6:i386 libpng16-16:i386 libfontconfig1:i386 libexpat1:i386 \
        libgl1:i386 libgl1-mesa-dri:i386 libsdl2-2.0-0:i386 \
        libxcomposite1:i386 libxinerama1:i386 libxrandr2:i386 libxcursor1:i386 \
        libxi6:i386 libgnutls30:i386 \
        libfreetype6 libpng16-16 libfontconfig1 libcups2 \
        >/dev/null
    LAUNCHER="box86 /usr/lib/wine/wine"
    ;;
arm64)
    dpkg --add-architecture armhf
    dpkg --add-architecture i386
    apt-get update >/dev/null
    apt-get install -y --no-install-recommends \
        crossbuild-essential-armhf cmake git python3 \
        libc6:armhf libstdc++6:armhf \
        wine32:i386 \
        libfreetype6:i386 libpng16-16:i386 libfontconfig1:i386 libexpat1:i386 \
        libgl1:i386 libgl1-mesa-dri:i386 libsdl2-2.0-0:i386 \
        libxcomposite1:i386 libxinerama1:i386 libxrandr2:i386 libxcursor1:i386 \
        libxi6:i386 libgnutls30:i386 \
        libgl1:armhf libglx-mesa0:armhf libgl1-mesa-dri:armhf \
        libxcursor1:armhf libxrandr2:armhf libxi6:armhf \
        libxcomposite1:armhf libxinerama1:armhf libxxf86vm1:armhf \
        libxrender1:armhf libxfixes3:armhf libxext6:armhf libx11-6:armhf \
        libfreetype6:armhf libfontconfig1:armhf libpng16-16:armhf \
        >/dev/null

    cd /tmp
    git clone --depth 1 https://github.com/ptitSeb/box86 >/dev/null 2>&1
    cd box86 && mkdir -p build && cd build
    cmake .. -DARM_DYNAREC=1 -DCMAKE_C_COMPILER=arm-linux-gnueabihf-gcc \
             -DCMAKE_BUILD_TYPE=Release >/dev/null
    make -j"\$(nproc)" >/dev/null
    install -m755 box86 /usr/local/bin/box86
    LAUNCHER="box86 /usr/lib/wine/wine"
    ;;
fex)
    apt-get install -y --no-install-recommends gpg-agent >/dev/null
    echo "deb [trusted=yes] http://ppa.launchpad.net/fex-emu/fex/ubuntu jammy main" \
        > /etc/apt/sources.list.d/fex.list
    dpkg --add-architecture i386
    apt-get update >/dev/null
    apt-get install -y --no-install-recommends \
        fex-emu-armv8.4 wine32:i386 \
        libfreetype6:i386 libpng16-16:i386 libfontconfig1:i386 libexpat1:i386 \
        libgl1:i386 libgl1-mesa-dri:i386 libsdl2-2.0-0:i386 \
        libxcomposite1:i386 libxinerama1:i386 libxrandr2:i386 libxcursor1:i386 \
        libxi6:i386 libgnutls30:i386 \
        >/dev/null
    LAUNCHER="FEXInterpreter /usr/lib/wine/wine"
    ;;
esac

cd /tmp
i686-w64-mingw32-gcc -o d3d9_init.exe   d3d9_init.c   -ld3d9 -lgdi32 -luser32 -lkernel32 -mwindows -static-libgcc
i686-w64-mingw32-gcc -o d3d9_render.exe d3d9_render.c -ld3d9 -lgdi32 -luser32 -lkernel32 -mwindows -static-libgcc
i686-w64-mingw32-gcc -o d3d9_dinput.exe d3d9_dinput.c -ld3d9 -ldinput8 -ldxguid -lgdi32 -luser32 -lkernel32 -mwindows -static-libgcc

Xvfb :99 -screen 0 800x480x24 -ac &
sleep 2
export DISPLAY=:99
export WINEPREFIX=/root/.wine_test
export WINEDLLOVERRIDES="mscoree=d;mshtml=d"
export WINEDEBUG=-all
export BOX86_NOBANNER=1
mkdir -p \$WINEPREFIX
\$LAUNCHER wineboot --init >/dev/null 2>&1 || true

echo "=== d3d9_init ==="
rm -f /tmp/d3d9_init.log
\$LAUNCHER /tmp/d3d9_init.exe >/dev/null 2>&1 || true
cat /tmp/d3d9_init.log

echo "=== d3d9_dinput ==="
rm -f /tmp/d3d9_dinput.log
\$LAUNCHER /tmp/d3d9_dinput.exe >/dev/null 2>&1 || true
cat /tmp/d3d9_dinput.log

echo "=== d3d9_render 1200 frames ==="
rm -f /tmp/d3d9_render.log
{ time \$LAUNCHER /tmp/d3d9_render.exe 1200 >/dev/null 2>&1; } 2>&1 | grep real
cat /tmp/d3d9_render.log
EOF

#!/bin/bash
# touhou-kiosk installer.
#
# Drops scripts into /home/$USER/touhou-kiosk/, places systemd units, and
# installs apt dependencies. Does NOT install Hangover Wine — that's a
# separate step, see README for the Hangover repo link.
#
# Usage:
#   sudo ./install.sh                  # install for user 'ubuntu'
#   sudo TARGET_USER=pi ./install.sh   # install for a different user
#
# After this finishes:
#   1. Drop your legal th12.exe + data files into /home/$USER/games/th12/
#   2. Copy etc/touhou-kiosk.default.example to /etc/default/touhou-kiosk
#      and tune THRESHOLD/HOOK to taste
#   3. systemctl enable --now touhou-gate.service
#   4. systemctl enable --now touhou-kiosk.service

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "[!] install.sh must run as root (use sudo)" >&2
    exit 1
fi

TARGET_USER="${TARGET_USER:-ubuntu}"
TARGET_HOME="$(getent passwd "${TARGET_USER}" | cut -d: -f6)"
if [ -z "${TARGET_HOME}" ] || [ ! -d "${TARGET_HOME}" ]; then
    echo "[!] user '${TARGET_USER}' has no home directory" >&2
    exit 1
fi

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
DEST_DIR="${TARGET_HOME}/touhou-kiosk"
GAME_DIR="${TARGET_HOME}/games/th12"

echo "=== touhou-kiosk installer ==="
echo "source:   ${SRC_DIR}"
echo "user:     ${TARGET_USER} (home: ${TARGET_HOME})"
echo "install:  ${DEST_DIR}"
echo "game:     ${GAME_DIR} (you supply th12.exe yourself)"
echo

echo "[+] apt dependencies"
apt-get update
apt-get install -y --no-install-recommends \
    xinit xserver-xorg-video-fbdev xserver-xorg-input-evdev \
    xdotool x11-xserver-utils \
    python3 python3-tk python3-evdev \
    psmisc procps \
    gcc-mingw-w64-i686

echo "[+] copying scripts to ${DEST_DIR}"
mkdir -p "${DEST_DIR}" "${GAME_DIR}"
cp -r "${SRC_DIR}/scripts/." "${DEST_DIR}/"
chown -R "${TARGET_USER}:${TARGET_USER}" "${DEST_DIR}" "${TARGET_HOME}/games"
chmod +x "${DEST_DIR}/launch_th12.sh"
find "${DEST_DIR}" -name "*.py" -exec chmod +x {} \;

echo "[+] staging TH12 stage-switch helper into ${GAME_DIR}"
# The watcher and cold-boot randomizer load these from TH12's working
# directory, not from the helper-script directory.
install -o "${TARGET_USER}" -g "${TARGET_USER}" -m 0755 \
    "${SRC_DIR}/scripts/th12_stageswitch/th12_stageswitch_inject.exe" \
    "${GAME_DIR}/th12_stageswitch_inject.exe"
install -o "${TARGET_USER}" -g "${TARGET_USER}" -m 0644 \
    "${SRC_DIR}/scripts/th12_stageswitch/th12_stageswitch_v4.dll" \
    "${GAME_DIR}/th12_stageswitch_v4.dll"

if [ "${TARGET_HOME}" != "/home/ubuntu" ]; then
    echo "[!] WARNING: scripts have hard-coded /home/ubuntu paths in places"
    echo "    (systemd units, HOOK strings, /opt/touhou-hangover defaults)."
    echo "    Edit ${DEST_DIR}/launch_th12.sh and systemd/*.service"
    echo "    to point at ${TARGET_HOME} before enabling the service."
fi

echo "[+] installing systemd units"
cp "${SRC_DIR}/systemd/"*.service /etc/systemd/system/
if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload
else
    echo "    [!] systemctl not available — units copied but not registered."
    echo "    [!] (this is fine in a container; on a real OPi systemd is always present)"
fi

echo "[+] env file"
if [ ! -e /etc/default/touhou-kiosk ]; then
    cp "${SRC_DIR}/etc/touhou-kiosk.default.example" /etc/default/touhou-kiosk
    echo "    wrote /etc/default/touhou-kiosk from template"
else
    echo "    /etc/default/touhou-kiosk already exists — left alone"
fi

echo "[+] sudoers — allow ${TARGET_USER} passwordless sudo for the kiosk's needs"
SUDOERS_FILE="/etc/sudoers.d/touhou-kiosk"
mkdir -p /etc/sudoers.d
chmod 0750 /etc/sudoers.d
if [ ! -e "${SUDOERS_FILE}" ]; then
    cat > "${SUDOERS_FILE}" <<EOF
# touhou-kiosk kiosk: passwordless sudo for the operations the launcher needs.
# The kiosk service runs as ${TARGET_USER} and shells out to root for killall,
# wineserver -k, xrandr-on-tty1, /proc/<pid>/mem reads, and systemctl restart.
${TARGET_USER} ALL=(ALL) NOPASSWD: /usr/bin/killall, /usr/bin/wineserver, /usr/bin/xrandr, /usr/bin/xdotool, /usr/bin/systemctl, /usr/bin/python3, /opt/touhou-hangover/usr/bin/wine, /opt/touhou-hangover/usr/bin/wineserver
EOF
    chmod 0440 "${SUDOERS_FILE}"
    visudo -c -f "${SUDOERS_FILE}" >/dev/null
    echo "    wrote ${SUDOERS_FILE}"
else
    echo "    ${SUDOERS_FILE} already exists — left alone"
fi

echo
echo "=== install complete ==="
echo
echo "Next steps:"
echo "  1. Install Hangover Wine 11.4 to /opt/touhou-hangover/"
echo "  2. Drop th12.exe and its data files into ${GAME_DIR}/"
echo "  3. Edit /etc/default/touhou-kiosk (THRESHOLD, HOOK, etc.)"
echo "  4. systemctl enable --now touhou-gate.service"
echo "  5. systemctl enable --now touhou-kiosk.service"
echo
echo "Logs:"
echo "  journalctl -u touhou-kiosk.service -f"
echo "  tail -f /tmp/watcher.log"

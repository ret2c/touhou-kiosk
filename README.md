# Touhou Kiosk
Running TH12: UFO (x86_32) on an Orange Pi 5 (aarch64) in a custom kiosk mode.

## Overview
- **Translation stack:** TH12 (32-bit x86 PE, 2009) runs under Hangover wine + Box64. Box64 JITs x86 → aarch64 (loaded as `wowbox64.dll` via wine's new-wow64 loader); wined3d translates D3D9 → OpenGL → Mesa → Panfrost on the Mali-G610
- **Score watching:** a python daemon reads TH12's score / stage / frame / lives globals from `/proc/<pid>/mem` at fixed offsets
- **Hook firing:** crossing the score threshold fires a configurable shell command and opens an arm window of `GATE_WINDOW` seconds. A separate gate daemon enforces default-deny + heartbeat-deadman so a watcher crash can't leave the hook armed
- **Stage resetting:** each threshold cross triggers a stage switch via an injected DLL that drives TH12's own teardown + init functions from the main thread
- **Recovery:** the watcher's reset ladder (stage_switch → native restart → systemctl) handles in-game stalls; bash supervisor respawns the watcher; systemd's `Restart=always` brings the whole service back.

## Quickstart
```bash
# On the OPi:
git clone https://github.com/ret2c/touhou-kiosk.git
cd touhou-kiosk
sudo ./install.sh

# Drop your TH12 install into /home/ubuntu/games/th12/
# (th12.exe + th12.dat + everything else)

sudo cp etc/touhou-kiosk.default.example /etc/default/touhou-kiosk
sudo systemctl enable --now touhou-gate.service
sudo systemctl enable --now touhou-kiosk.service
```
Afterwards, the kiosk will cold-boot into stage 1 in ~20ish seconds, the score overlay will pin itself over the playfield and the set score threshold (200,000) will fire the hook.

To refresh any config changes:
```bash
sudo systemctl restart touhou-kiosk.service
```

## Configuration
You can find config knobs in `/etc/default/touhou-kiosk`.

- `THRESHOLD` — scoring gate. Defaults to 200,000 in production, 10,000 in debug mode
- `GATE_WINDOW` — seconds the on-screen flash stay open after a threshold cross.
- `HOOK` — shell command run when the threshold is crossed. By default this arms the gate daemon.
- `TH12_STAGESWITCH=1` / `TH12_STAGESWITCH_STAGES=1,2,3,4,5,6` — after a threshold cross, switch into a random stage from the pool instead of restarting the same stage.
- `DEBUG_MODE=` — enables debug overlays and changes the gate-window behavior so you can observe the full arm/disarm path.

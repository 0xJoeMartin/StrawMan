#!/usr/bin/env bash
# StrawMan installer for Raspberry Pi 5 (Raspberry Pi OS 64-bit, Bookworm or Trixie).
#
#   git clone https://github.com/<user>/StrawMan.git ~/scarecrow
#   cd ~/scarecrow && ./install.sh
#
# Safe to run again: each step checks first and only changes what's missing.
# Run it as your normal user (it uses sudo where it has to).
#
#   --dry-run     show what would change, change nothing
#   --yes         don't ask before rebooting at the end
#   --no-service  don't install/start the systemd service (manual runs only)
#   --no-radar    don't turn on the serial port for the LD2450 radar
#   --dir PATH    install into PATH instead of this script's folder

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRY=0 YES=0 SERVICE=1 RADAR=1
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --yes|-y) YES=1 ;;
    --no-service) SERVICE=0 ;;
    --no-radar) RADAR=0 ;;
    --dir) DIR="$(cd "$2" && pwd)"; shift ;;
    -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1 (try --help)"; exit 1 ;;
  esac
  shift
done

APT_PACKAGES=(git python3-venv python3-dev build-essential portaudio19-dev ffmpeg i2c-tools espeak-ng)
GROUPS_NEEDED=(i2c gpio dialout audio video)
BOOT_CONFIG=/boot/firmware/config.txt
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/scarecrow.service"
STAMP="$(date +%Y%m%d-%H%M%S)"
REBOOT_WHY=()
CHANGED=()

bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
note() { printf '  \033[33m•\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; }
run() {
  if [ "$DRY" = 1 ]; then printf '  would run: %s\n' "$*"; else "$@"; fi
}
changed() { CHANGED+=("$1"); }

# ---------------------------------------------------------------- checks first
if [ "$(id -u)" = 0 ]; then
  echo "Run this as your normal user, not with sudo — it asks for sudo when it needs it."
  exit 1
fi
[ -f "$DIR/scarecrow.py" ] || { echo "No scarecrow.py in $DIR — run this from the StrawMan folder."; exit 1; }

bold "StrawMan installer$([ "$DRY" = 1 ] && echo ' (dry run: nothing will be changed)')"
echo "  Folder: $DIR"
MODEL="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown)"
echo "  Board:  $MODEL"
. /etc/os-release 2>/dev/null || true
echo "  OS:     ${PRETTY_NAME:-unknown}"
case "$MODEL" in *"Raspberry Pi 5"*) ;; *) note "This is built for a Raspberry Pi 5; carrying on anyway." ;; esac
[ "$(uname -m)" = aarch64 ] || note "Expected 64-bit Raspberry Pi OS (aarch64), found $(uname -m)."

# ------------------------------------------------------------ 1. system packages
bold "1/8  System packages"
MISSING=()
for p in "${APT_PACKAGES[@]}"; do
  dpkg -s "$p" >/dev/null 2>&1 || MISSING+=("$p")
done
HAILO_FOUND=0
if [ -e /dev/hailo0 ] || { lspci 2>/dev/null || true; } | grep -i hailo >/dev/null; then
  HAILO_FOUND=1
  dpkg -s hailo-all >/dev/null 2>&1 || MISSING+=(hailo-all)
fi
if [ ${#MISSING[@]} -eq 0 ]; then
  ok "All installed"
else
  echo "  To install: ${MISSING[*]}"
  run sudo apt-get update
  run sudo apt-get install -y "${MISSING[@]}"
  changed "installed ${MISSING[*]}"
  for p in "${MISSING[@]}"; do [ "$p" = hailo-all ] && REBOOT_WHY+=("the Hailo driver was installed"); done
fi
if [ "$HAILO_FOUND" = 1 ]; then ok "AI HAT+ (Hailo) found — fast person detection"
else note "No AI HAT+ found — person detection will run on the CPU (works, choppier tracking)"; fi

# ------------------------------------------------------------ 2. I2C + serial port
bold "2/8  I²C (servo board) and serial port (radar)"
if command -v raspi-config >/dev/null; then
  if [ "$(sudo raspi-config nonint get_i2c)" = 0 ]; then
    ok "I²C is on"
  else
    run sudo raspi-config nonint do_i2c 0
    changed "turned on I²C"; REBOOT_WHY+=("I²C was turned on")
  fi
else
  note "raspi-config not found — turn on I²C yourself (dtparam=i2c_arm=on in $BOOT_CONFIG)"
fi
if [ "$RADAR" = 1 ]; then
  if [ ! -f "$BOOT_CONFIG" ]; then
    note "$BOOT_CONFIG not found — add dtparam=uart0=on yourself for the radar"
  elif grep -qE '^[[:space:]]*dtparam=uart0=on' "$BOOT_CONFIG"; then
    ok "Serial port on pins 8/10 is on"
  else
    echo "  Adding dtparam=uart0=on to $BOOT_CONFIG (backup: config.txt.strawman-$STAMP)"
    run sudo cp -p "$BOOT_CONFIG" "$BOOT_CONFIG.strawman-$STAMP"
    if [ "$DRY" = 0 ]; then
      printf '\n[all]\n# StrawMan: LD2450 radar on GPIO 14/15 (/dev/ttyAMA0)\ndtparam=uart0=on\n' | sudo tee -a "$BOOT_CONFIG" >/dev/null
    fi
    changed "turned on the serial port"; REBOOT_WHY+=("the serial port was turned on")
  fi
else
  note "Skipping the radar's serial port (--no-radar)"
fi

# ------------------------------------------------------------ 3. groups
bold "3/8  Hardware access for $USER"
ADD=()
for g in "${GROUPS_NEEDED[@]}"; do
  getent group "$g" >/dev/null || continue
  [[ " $(id -nG "$USER") " == *" $g "* ]] || ADD+=("$g")
done
if [ ${#ADD[@]} -eq 0 ]; then
  ok "Already in ${GROUPS_NEEDED[*]}"
else
  run sudo usermod -aG "$(IFS=,; echo "${ADD[*]}")" "$USER"
  changed "added $USER to ${ADD[*]}"; REBOOT_WHY+=("$USER joined ${ADD[*]}")
fi

# ------------------------------------------------------------ 4. venv
bold "4/8  Python environment"
VENV="$DIR/venv"
FRESH=0
if [ -x "$VENV/bin/python" ]; then
  ok "venv exists"
  if [ "$HAILO_FOUND" = 1 ] && ! grep -q 'include-system-site-packages = true' "$VENV/pyvenv.cfg"; then
    note "This venv can't see the Hailo libraries. Rename it (mv venv venv.old) and run this again."
  fi
else
  # --system-site-packages: the Hailo libraries come from apt (hailo-all), not pip
  run python3 -m venv --system-site-packages "$VENV"
  FRESH=1; changed "created the venv"
fi

# ------------------------------------------------------------ 5. Python packages
bold "5/8  Python packages"
PIP=("$VENV/bin/pip" install -r "$DIR/requirements.txt")
[ -f "$DIR/constraints.txt" ] && PIP+=(-c "$DIR/constraints.txt")   # the tested versions
# PyTorch first, from its CPU-only index: the PyPI build drags in ~3 GB of
# NVIDIA CUDA libraries that do nothing on a Pi.
TORCH=()
if [ "$FRESH" = 1 ] || ! "$VENV/bin/python" -c "import torch" 2>/dev/null; then
  for t in torch torchaudio torchvision; do
    v="$(sed -n "s/^$t==//p" "$DIR/constraints.txt" 2>/dev/null)"
    TORCH+=("$t${v:+==$v}")
  done
fi
if [ ${#TORCH[@]} -gt 0 ]; then
  note "Installing everything (first time: 5–15 minutes, depending on your internet)"
  run "$VENV/bin/pip" install --index-url https://download.pytorch.org/whl/cpu "${TORCH[@]}"
  run "${PIP[@]}"
  note "If pip printed a \"dependency conflicts\" ERROR about packages like types-seaborn or hailort,"
  note "that's about Raspberry Pi OS's own packages, not StrawMan's — safe to ignore."
  changed "installed Python packages"
else
  TODO="$("${PIP[@]}" --dry-run 2>/dev/null | sed -n 's/^Would install //p' || true)"
  if [ -z "$TODO" ]; then
    ok "All installed"
  else
    echo "  To install: $TODO"
    run "${PIP[@]}"
    changed "installed Python packages"
  fi
fi

# ------------------------------------------------------------ 6. service
bold "6/8  Start at boot"
if [ "$SERVICE" = 0 ]; then
  note "Skipped (--no-service). Run by hand: cd $DIR && venv/bin/python scarecrow.py"
else
  if [ "$DIR" = "$HOME/scarecrow" ]; then WD="%h/scarecrow"; else WD="$DIR"; fi
  # A *user* service (not a system one) so audio reaches the desktop sound
  # server (PipeWire). Linger starts it at boot without anyone logging in.
  WANT="[Unit]
Description=StrawMan talking scarecrow
After=pipewire.service pipewire-pulse.service wireplumber.service
Wants=pipewire.service pipewire-pulse.service wireplumber.service

[Service]
Type=simple
WorkingDirectory=$WD
# Wait up to 30s for a default route so the LLM/TTS/STT backends start healthy
ExecStartPre=/bin/sh -c \"for i in \$(seq 30); do ip route get 1.1.1.1 >/dev/null 2>&1 && exit 0; sleep 1; done; exit 0\"
ExecStart=$WD/venv/bin/python -u scarecrow.py
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=10
KillSignal=SIGINT
TimeoutStopSec=15

[Install]
WantedBy=default.target"
  UNIT_CHANGED=0
  if [ -f "$UNIT" ] && [ "$(cat "$UNIT")" = "$WANT" ]; then
    ok "Service file is up to date"
  else
    if [ -f "$UNIT" ]; then
      echo "  Updating $UNIT (backup: scarecrow.service.bak-$STAMP)"
      run cp -p "$UNIT" "$UNIT.bak-$STAMP"
    else
      echo "  Writing $UNIT"
    fi
    if [ "$DRY" = 0 ]; then
      mkdir -p "$UNIT_DIR"
      printf '%s\n' "$WANT" > "$UNIT"
    fi
    run systemctl --user daemon-reload
    UNIT_CHANGED=1; changed "wrote the service file"
  fi
  if systemctl --user is-enabled scarecrow >/dev/null 2>&1; then ok "Service enabled"
  else run systemctl --user enable scarecrow; changed "enabled the service"; fi
  if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" = yes ]; then ok "Starts at boot (linger on)"
  else run sudo loginctl enable-linger "$USER"; changed "turned on linger"; fi
  if [ ${#REBOOT_WHY[@]} -gt 0 ]; then
    note "Will start after the reboot"
  elif ! systemctl --user is-active scarecrow >/dev/null 2>&1; then
    run systemctl --user start scarecrow; changed "started the scarecrow"
  elif [ "$UNIT_CHANGED" = 1 ]; then
    run systemctl --user restart scarecrow; changed "restarted the scarecrow"
  else
    ok "Running"
  fi
fi

# ------------------------------------------------------------ 7. network
bold "7/8  Wi-Fi power save and network watchdog"
# put SRC DEST MODE: copy only if it differs; true when it changed something
put() {
  if [ -f "$2" ] && cmp -s "$1" "$2"; then return 1; fi
  echo "  Writing $2"
  run sudo install -m "$3" "$1" "$2"
}
if [ "$SERVICE" = 0 ]; then
  note "Skipped (--no-service)"
elif ! command -v nmcli >/dev/null; then
  note "NetworkManager not found — skipping (the watchdog uses nmcli to reset Wi-Fi)"
else
  # With power save on, the Pi's Wi-Fi can go dead while still looking connected
  if put "$DIR/system/50-strawman-wifi-powersave.conf" /etc/NetworkManager/conf.d/50-strawman-wifi-powersave.conf 644; then
    run sudo systemctl reload NetworkManager
    changed "turned off Wi-Fi power save"
  fi
  # The config applies on the next connect; this turns it off right now
  if [ -e /sys/class/net/wlan0 ] && sudo /usr/sbin/iw dev wlan0 get power_save 2>/dev/null | grep -q ': on'; then
    run sudo /usr/sbin/iw dev wlan0 set power_save off
    changed "turned off Wi-Fi power save on wlan0"
  elif [ -e /sys/class/net/wlan0 ]; then
    ok "Wi-Fi power save is off"
  fi
  NW=0
  put "$DIR/system/strawman-netwatch.sh" /usr/local/sbin/strawman-netwatch 755 && NW=1
  if put "$DIR/system/strawman-netwatch.service" /etc/systemd/system/strawman-netwatch.service 644; then
    run sudo systemctl daemon-reload
    NW=1
  fi
  if ! systemctl is-enabled strawman-netwatch >/dev/null 2>&1; then
    run sudo systemctl enable --now strawman-netwatch
    changed "started the network watchdog"
  elif [ "$NW" = 1 ]; then
    run sudo systemctl restart strawman-netwatch
    changed "updated the network watchdog"
  else
    ok "Network watchdog running (resets Wi-Fi, then reboots, if the router stops answering)"
  fi
fi

# ------------------------------------------------------------ 8. hardware check
bold "8/8  What's plugged in"
if [ -e /dev/i2c-1 ] && { /usr/sbin/i2cdetect -y 1 2>/dev/null || true; } | grep -E '^40:[[:space:]]+40' >/dev/null; then
  ok "Servo board (PCA9685) at 0x40"
else
  bad "No servo board at 0x40 — check its 4 wires (or reboot first if I²C was just turned on). He'll run without head or jaw."
fi
if [ "$HAILO_FOUND" = 1 ]; then
  if [ -e /dev/hailo0 ]; then ok "AI HAT+ ready (/dev/hailo0)"; else note "AI HAT+ found; /dev/hailo0 appears after the reboot"; fi
fi
if [ "$RADAR" = 1 ]; then
  if [ -e /dev/ttyAMA0 ]; then ok "Radar serial port /dev/ttyAMA0 (the radar itself shows up on the dashboard)"
  else note "/dev/ttyAMA0 appears after the reboot (radar is optional)"; fi
fi
CAMS=""
for d in /sys/class/video4linux/video*; do
  [ -e "$d/name" ] || continue
  n="$(cat "$d/name")"
  case "$n" in pispbe*|rpi-hevc*|rp1-cfe*|bcm2835*) continue ;; esac
  case "$CAMS" in *"$n"*) ;; *) CAMS="${CAMS:+$CAMS, }$n" ;; esac
done
if [ -n "$CAMS" ]; then ok "Camera: $CAMS"; else bad "No USB camera found — plug one in"; fi
MICS="$(arecord -l 2>/dev/null | sed -n 's/^card [0-9]*: [^[]*\[\([^]]*\)\].*/\1/p' | sort -u | paste -sd, - | sed 's/,/, /g' || true)"
if [ -n "$MICS" ]; then ok "Recording devices: $MICS"; else bad "No microphone found — he'll talk but not listen"; fi
OUTS="$(aplay -l 2>/dev/null | grep -vi hdmi | sed -n 's/^card [0-9]*: [^[]*\[\([^]]*\)\].*/\1/p' | sort -u | paste -sd, - | sed 's/,/, /g' || true)"
if [ -n "$OUTS" ]; then ok "Speaker outputs: $OUTS"; else bad "No speaker output found (the Pi 5 has no headphone jack — use a USB audio adapter)"; fi

# ------------------------------------------------------------ done
bold "Done"
if [ ${#CHANGED[@]} -eq 0 ]; then
  ok "Nothing needed changing"
else
  for c in "${CHANGED[@]}"; do echo "  - ${c}$([ "$DRY" = 1 ] && echo ' (dry run)')"; done
fi
IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
[ -n "$IP" ] || IP="<the Pi's IP address>"
echo
echo "  Dashboard: https://$IP:5000"
echo "  Your browser will warn about the certificate (he makes his own) — continue anyway."
echo "  If he isn't set up yet, the first visit starts the setup wizard."
if [ ${#REBOOT_WHY[@]} -gt 0 ]; then
  echo
  echo "  A reboot is needed: $(IFS=';'; echo "${REBOOT_WHY[*]}" | sed 's/;/; /g')."
  if [ "$DRY" = 1 ]; then
    :
  elif [ "$YES" = 1 ]; then
    sudo reboot
  elif [ -t 0 ]; then
    read -r -p "  Reboot now? [y/N] " a
    case "$a" in [yY]*) sudo reboot ;; *) echo "  Run 'sudo reboot' when you're ready." ;; esac
  else
    echo "  Run 'sudo reboot' when you're ready."
  fi
fi

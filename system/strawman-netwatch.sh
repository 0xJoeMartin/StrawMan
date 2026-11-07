#!/usr/bin/env bash
# StrawMan network watchdog. Installed by install.sh as
# /usr/local/sbin/strawman-netwatch, run as root by strawman-netwatch.service.
#
# The Pi's Wi-Fi sometimes goes dead while NetworkManager still thinks it's
# connected (Oct 2026: DNS failing, no ARP replies, nothing logged). Pings the
# router every 30 s. When it stops answering:
#   3 min  turn Wi-Fi off and on
#   6 min  reload the Wi-Fi driver
#   9 min  reboot (only after 15 min of uptime and at most once an hour,
#          so a dead router doesn't make him reboot forever)
# Logs go to the journal: journalctl -u strawman-netwatch
#
# The NETWATCH_* variables are for test_netwatch.py; leave them unset.

INTERVAL=${NETWATCH_INTERVAL:-30}
RESET_AFTER=${NETWATCH_RESET_AFTER:-6}      # failed checks in a row
RELOAD_AFTER=${NETWATCH_RELOAD_AFTER:-12}
REBOOT_AFTER=${NETWATCH_REBOOT_AFTER:-18}
MIN_UPTIME=${NETWATCH_MIN_UPTIME:-900}      # seconds
REBOOT_GAP=${NETWATCH_REBOOT_GAP:-3600}
STATE_DIR=${NETWATCH_STATE_DIR:-/var/lib/strawman-netwatch}
UPTIME_FILE=${NETWATCH_UPTIME_FILE:-/proc/uptime}
CHECK=${NETWATCH_CHECK:-}                   # test hook: command that says up/down
DRY=${NETWATCH_DRY:-0}                      # 1 = print actions, don't do them
MAX_CHECKS=${NETWATCH_MAX_CHECKS:-0}        # 0 = run forever

log() { echo "$*"; }

act() {
  if [ "$DRY" = 1 ]; then log "would run: $*"; else "$@"; fi
}

gateway() {
  ip route show default 2>/dev/null | awk '/^default/ {print $3; exit}'
}

reachable() {
  if [ -n "$CHECK" ]; then $CHECK; return; fi
  gw="$(gateway)"
  [ -n "$gw" ] || return 1
  # Two tries so one lost packet isn't a failure
  ping -c 1 -W 3 "$gw" >/dev/null 2>&1 || ping -c 1 -W 3 "$gw" >/dev/null 2>&1
}

uptime_s() { cut -d. -f1 "$UPTIME_FILE"; }

last_reboot() { cat "$STATE_DIR/last-reboot" 2>/dev/null || echo 0; }

reset_wifi() {
  log "Router not answering for $((down * INTERVAL)) s — turning Wi-Fi off and on"
  act nmcli radio wifi off
  act sleep 3
  act nmcli radio wifi on
}

reload_driver() {
  log "Still not answering after $((down * INTERVAL)) s — reloading the Wi-Fi driver"
  act modprobe -r brcmfmac_wcc brcmfmac
  act sleep 2
  act modprobe brcmfmac
}

maybe_reboot() {
  up="$(uptime_s)"
  since=$(( $(date +%s) - $(last_reboot) ))
  if [ "$up" -lt "$MIN_UPTIME" ]; then
    why="only up $((up / 60)) min"
  elif [ "$since" -lt "$REBOOT_GAP" ]; then
    why="already rebooted $((since / 60)) min ago"
  else
    log "Still not answering after $((down * INTERVAL)) s — rebooting"
    mkdir -p "$STATE_DIR" && date +%s > "$STATE_DIR/last-reboot"
    act systemctl reboot
    # If a clean reboot hangs (e.g. the SD card is stuck), force it
    if [ "$DRY" != 1 ]; then
      sync & sleep 180
      echo b > /proc/sysrq-trigger
    fi
    return
  fi
  log "Not rebooting ($why) — will keep resetting Wi-Fi"
}

fails=0 down=0 checks=0   # fails restarts the steps; down is how long it's really been
log "Watching the router every ${INTERVAL} s (gateway: $(gateway || true))"
while :; do
  if reachable; then
    [ "$down" -ge "$RESET_AFTER" ] && log "Router answering again after $((down * INTERVAL)) s"
    fails=0 down=0
  else
    fails=$((fails + 1)) down=$((down + 1))
    if [ "$fails" -eq "$RESET_AFTER" ]; then
      reset_wifi
    elif [ "$fails" -eq "$RELOAD_AFTER" ]; then
      reload_driver
    elif [ "$fails" -ge "$REBOOT_AFTER" ]; then
      maybe_reboot
      # Not allowed to reboot yet: go round the Wi-Fi resets again
      fails=$((RESET_AFTER - 1))
    fi
  fi
  checks=$((checks + 1))
  [ "$MAX_CHECKS" -gt 0 ] && [ "$checks" -ge "$MAX_CHECKS" ] && break
  sleep "$INTERVAL"
done

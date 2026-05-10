#!/usr/bin/env bash
set -Eeuo pipefail

SSID="${MW_SETUP_SSID:-The Morse Whisperer}"
CON_NAME="${MW_SETUP_CONN:-morse-whisperer-setup-hotspot}"
IFACE="${MW_WIFI_IFACE:-wlan0}"
AP_ADDR="${MW_SETUP_ADDR:-10.42.0.1/24}"
AP_IP="${MW_SETUP_IP:-10.42.0.1}"

log() {
  echo "[mw-network] $(date '+%Y-%m-%d %H:%M:%S') $*"
}

have_nmcli() {
  command -v nmcli >/dev/null 2>&1
}

wifi_device_exists() {
  nmcli -t -f DEVICE,TYPE device status 2>/dev/null | grep -q "^${IFACE}:wifi$"
}

has_default_route() {
  ip route show default 2>/dev/null | grep -q '^default '
}

is_hotspot_active() {
  nmcli -t -f NAME,DEVICE connection show --active 2>/dev/null | grep -q "^${CON_NAME}:${IFACE}$"
}

active_connection_name() {
  nmcli -t -f NAME,DEVICE connection show --active 2>/dev/null | awk -F: -v dev="$IFACE" '$2==dev {print $1; exit}'
}

status() {
  echo "ssid=$SSID"
  echo "connection_name=$CON_NAME"
  echo "iface=$IFACE"
  echo "ap_addr=$AP_ADDR"
  echo "ap_ip=$AP_IP"
  echo "nmcli=$(command -v nmcli || true)"
  echo "networkmanager_active=$(systemctl is-active NetworkManager 2>/dev/null || true)"
  echo "wifi_device_exists=$(wifi_device_exists && echo yes || echo no)"
  echo "default_route=$(has_default_route && echo yes || echo no)"
  echo "hotspot_active=$(is_hotspot_active && echo yes || echo no)"
  echo "active_wifi_connection=$(active_connection_name || true)"
  echo
  nmcli device status 2>/dev/null || true
}

ensure_hotspot_profile() {
  if nmcli -t -f NAME connection show 2>/dev/null | grep -qx "$CON_NAME"; then
    log "Hotspot profile already exists: $CON_NAME"
  else
    log "Creating hotspot profile: $CON_NAME"
    nmcli connection add \
      type wifi \
      ifname "$IFACE" \
      con-name "$CON_NAME" \
      autoconnect no \
      ssid "$SSID"
  fi

  log "Configuring open setup hotspot profile"
  nmcli connection modify "$CON_NAME" \
    802-11-wireless.mode ap \
    802-11-wireless.band bg \
    ipv4.method shared \
    ipv4.addresses "$AP_ADDR" \
    ipv6.method disabled \
    connection.autoconnect no \
    wifi-sec.key-mgmt none
}

start_hotspot() {
  if ! have_nmcli; then
    log "ERROR: nmcli not found"
    exit 1
  fi

  if ! wifi_device_exists; then
    log "ERROR: Wi-Fi device $IFACE not found or not managed by NetworkManager"
    exit 1
  fi

  ensure_hotspot_profile

  log "Starting setup hotspot SSID: $SSID"
  log "Setup URL will be: http://${AP_IP}:8080"
  nmcli connection up "$CON_NAME"
}

start_if_needed() {
  if has_default_route; then
    log "Default route exists; not starting setup hotspot"
    exit 0
  fi

  log "No default route found; starting setup hotspot"
  start_hotspot
}

stop_hotspot() {
  if is_hotspot_active; then
    log "Stopping setup hotspot"
    nmcli connection down "$CON_NAME" || true
  else
    log "Setup hotspot is not active"
  fi
}

delete_hotspot_profile() {
  stop_hotspot
  if nmcli -t -f NAME connection show 2>/dev/null | grep -qx "$CON_NAME"; then
    log "Deleting hotspot profile: $CON_NAME"
    nmcli connection delete "$CON_NAME"
  else
    log "No hotspot profile to delete"
  fi
}

dry_run() {
  echo "Would use:"
  echo "  SSID: $SSID"
  echo "  Interface: $IFACE"
  echo "  Connection profile: $CON_NAME"
  echo "  Hotspot IP: $AP_IP"
  echo
  if has_default_route; then
    echo "Current result: default route exists, so fallback would NOT start."
  else
    echo "Current result: no default route, so fallback WOULD start."
  fi
}

case "${1:-status}" in
  status)
    status
    ;;
  dry-run)
    dry_run
    ;;
  start)
    start_hotspot
    ;;
  start-if-needed)
    start_if_needed
    ;;
  stop)
    stop_hotspot
    ;;
  delete-profile)
    delete_hotspot_profile
    ;;
  *)
    echo "Usage: $0 {status|dry-run|start|start-if-needed|stop|delete-profile}"
    exit 2
    ;;
esac

#!/bin/sh
# Prepare a Jetson for OpenTraffic. Run once per unit, on the bench,
# with sudo -- the only step that needs root. Everything after it
# (deploy/install.sh, updates, the inspector service, choosing the
# controller) runs as the unit's user without sudo.
#
#   sudo LIDAR_IF=enP8p1s0 deploy/provision-host.sh
#
# Settings (environment):
#   LIDAR_IF      the port the LiDAR is wired to (required)
#   LIDAR_ADDR    this unit's address on it       default 192.168.0.25/24
#   UNIT_NAME     hostname, e.g. ot-main-and-5th  default: unchanged
#   OT_USER       who runs OpenTraffic            default: the sudo caller
#
# It does:
#   * Docker access for OT_USER (docker group) and lingering, so the
#     user's services run from boot without anyone logged in
#   * the UDP receive buffer the OS-1-128 stream needs
#   * the LiDAR port: a fixed address on the sensor subnet plus one
#     link-local address, so a sensor is reachable whether it was set
#     up with a static address or left on link-local; never a default
#     route, so it cannot steal the cabinet network's traffic
#   * the hostname, if given
#
# The cabinet/city port is left alone: DHCP by default, or set it as
# city IT asks (nmcli). Use a different subnet from the LiDAR port.
set -eu

[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }

OT_USER=${OT_USER:-${SUDO_USER:-}}
LIDAR_ADDR=${LIDAR_ADDR:-192.168.0.25/24}
LINK_LOCAL=${LINK_LOCAL:-169.254.0.25/16}

[ -n "$OT_USER" ] || { echo "set OT_USER to the account that runs OpenTraffic" >&2; exit 1; }
[ -n "${LIDAR_IF:-}" ] || {
    echo "set LIDAR_IF to the LiDAR port. Wired ports here:" >&2
    nmcli -t -f DEVICE,TYPE,STATE device | awk -F: '$2=="ethernet"{print "  "$1" ("$3")"}' >&2
    exit 1
}

say() { printf '\n== %s\n' "$*"; }

say "Docker access and lingering for $OT_USER"
command -v docker >/dev/null || { echo "Docker is not installed (JetPack: apt install docker.io docker-compose-v2)" >&2; exit 1; }
getent group docker >/dev/null || groupadd docker
usermod -aG docker "$OT_USER"
loginctl enable-linger "$OT_USER"

say "UDP receive buffer"
echo 'net.core.rmem_max=8388608' > /etc/sysctl.d/60-opentraffic.conf
sysctl -q --system
sysctl net.core.rmem_max

say "LiDAR port $LIDAR_IF: $LIDAR_ADDR + $LINK_LOCAL"
if nmcli -t -f NAME connection show | grep -qx opentraffic-lidar; then
    nmcli connection modify opentraffic-lidar connection.interface-name "$LIDAR_IF"
else
    nmcli connection add type ethernet con-name opentraffic-lidar ifname "$LIDAR_IF" >/dev/null
fi
nmcli connection modify opentraffic-lidar \
    connection.autoconnect yes connection.autoconnect-priority 100 \
    ipv4.method manual ipv4.addresses "$LIDAR_ADDR,$LINK_LOCAL" \
    ipv4.gateway "" ipv4.never-default yes ipv4.ignore-auto-dns yes \
    ipv6.method ignore
# Other profiles on the port would compete with this one at boot.
nmcli -t -f NAME,DEVICE connection show | while IFS=: read -r name device; do
    if [ "$device" = "$LIDAR_IF" ] && [ "$name" != opentraffic-lidar ]; then
        nmcli connection modify "$name" connection.autoconnect no
        echo "  autoconnect off for '$name'"
    fi
done
nmcli connection up opentraffic-lidar >/dev/null || echo "  (will come up when the cable is connected)"

if [ -n "${UNIT_NAME:-}" ]; then
    say "Hostname $UNIT_NAME"
    hostnamectl set-hostname "$UNIT_NAME"
fi

say "Done"
echo "Now, as $OT_USER (log out and in first if Docker access was just granted):"
echo "  deploy/install.sh"

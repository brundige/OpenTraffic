#!/bin/sh
# Install or update OpenTraffic on a unit. No sudo: run it as the
# unit's user, from the checkout, after deploy/provision-host.sh has
# prepared the host once.
#
#   deploy/install.sh              build, start the detector, install
#                                  the on-demand inspector on :8080
#
# Update the same way: git pull, then run it again. Zones, background,
# site settings and credentials in data/ are kept.
#
# INSPECTOR_PORT=8099 deploy/install.sh   another public port (testing)
set -eu

cd "$(dirname "$0")/.."
CHECKOUT=$(pwd)
PORT=${INSPECTOR_PORT:-8080}
UNITS="$HOME/.config/systemd/user"

say() { printf '\n== %s\n' "$*"; }
fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- checks

say "Checking the host"

docker info >/dev/null 2>&1 \
    || fail "cannot use Docker as $(id -un). Run deploy/provision-host.sh once (sudo), then log out and in."

if [ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null)" != "yes" ]; then
    fail "lingering is off, so the inspector would stop at logout. Run deploy/provision-host.sh once (sudo)."
fi

rmem=$(sysctl -n net.core.rmem_max)
[ "$rmem" -ge 8388608 ] \
    || echo "warning: net.core.rmem_max is $rmem; LiDAR packets will drop. Run deploy/provision-host.sh."

echo "ok: docker, lingering, receive buffer"

# ------------------------------------------------------------- detector

say "Building the image"
OPENTRAFFIC_VERSION=$(git describe --always --dirty 2>/dev/null || echo unknown) \
    docker compose build

if ! docker compose run --rm --no-deps -T detector python auth.py show 2>/dev/null | grep -q "Login:.*operator"; then
    say "Setting the operator password"
    if [ -t 0 ]; then
        docker compose run --rm --no-deps detector python auth.py set-password
    else
        echo "no login set and no terminal to ask on; run:"
        echo "  docker compose run --rm detector python auth.py set-password"
    fi
fi

say "Starting the detector"
docker compose up -d detector

# ------------------------------------------------------------- inspector

say "Installing the on-demand inspector (user units, port $PORT)"

for legacy in /etc/systemd/system/opentraffic-inspector-proxy.socket \
              /etc/systemd/system/opentraffic-inspector-proxy.service \
              /etc/systemd/system/opentraffic-inspector.service; do
    if [ -e "$legacy" ]; then
        echo "An older system-wide inspector is installed and holds port 8080. Remove it once with:"
        echo "  sudo sh -c 'systemctl disable --now opentraffic-inspector-proxy.socket opentraffic-inspector-proxy.service opentraffic-inspector.service; rm /etc/systemd/system/opentraffic-inspector*; systemctl daemon-reload'"
        echo "then run deploy/install.sh again."
        [ "$PORT" = 8080 ] && exit 1
        break
    fi
done

mkdir -p "$UNITS"

for unit in deploy/systemd-user/*; do
    sed -e "s|@CHECKOUT@|$CHECKOUT|g" -e "s|@PORT@|$PORT|g" "$unit" > "$UNITS/$(basename "$unit")"
done

systemctl --user daemon-reload

# An inspector still running from before this build would keep serving
# the old page until it idles out; stop it so the next connection
# starts the new image.
systemctl --user stop opentraffic-inspector-proxy.service opentraffic-inspector.service 2>/dev/null || true
docker compose --profile gui rm -sf inspector >/dev/null 2>&1 || true

systemctl --user enable opentraffic-inspector-proxy.socket >/dev/null
systemctl --user restart opentraffic-inspector-proxy.socket

# ----------------------------------------------------------------- done

say "Done"

echo "Inspector on port $PORT (starts on first connection), health on 8090."
echo "This unit's addresses -- use the one on the cabinet/city network:"
ip -4 -br addr show up | awk '$1 !~ /^(lo|docker|br-|veth|l4tbr)/ {
    for (i = 3; i <= NF; i++) { split($i, a, "/"); printf "  %-12s http://%s:'"$PORT"'/\n", $1, a[1] } }'
echo
echo "The sensor is found automatically; choose the controller in the"
echo "inspector's setup guide. Logs: docker logs -f traffic-detector"

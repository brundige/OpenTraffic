#!/bin/sh
# Install the on-demand inspector units on the Jetson.
# Run from the checkout at /opt/opentraffic, as root.
set -eu

cd "$(dirname "$0")/.."

if [ "$(pwd)" != "/opt/opentraffic" ]; then
    echo "Expected the checkout at /opt/opentraffic (the units point there); found $(pwd)." >&2
    exit 1
fi

install -m 0644 deploy/systemd/opentraffic-inspector-proxy.socket  /etc/systemd/system/
install -m 0644 deploy/systemd/opentraffic-inspector-proxy.service /etc/systemd/system/
install -m 0644 deploy/systemd/opentraffic-inspector.service       /etc/systemd/system/

systemctl daemon-reload
systemctl enable --now opentraffic-inspector-proxy.socket

echo "Inspector on demand at http://$(hostname -I | awk '{print $1}'):8080/"
systemctl --no-pager status opentraffic-inspector-proxy.socket | head -5

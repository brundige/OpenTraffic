"""
Run the inspector on its own, as the sidecar next to a running detector:

    python -m inspector                # profile from $OPENTRAFFIC_PROFILE
    python -m inspector --profile prod

On the Jetson, systemd starts this when someone connects and stops it
once they have gone (deploy/systemd-user/).
"""

import argparse
import signal
import sys

from inspector import InspectorServer
from settings import load_settings


def main() -> int:

    parser = argparse.ArgumentParser(description="OpenTraffic inspector")
    parser.add_argument("--profile", default=None)
    args = parser.parse_args()

    settings = load_settings(args.profile)

    server = InspectorServer(
        settings.auth_file,
        upstream=(settings.api_host, settings.api_port),
        host=settings.inspector_host,
        port=settings.inspector_port,
        session_hours=settings.session_hours,
    )

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    print(f"Inspector: {server.url} -> detector API {settings.api_host}:{settings.api_port}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())

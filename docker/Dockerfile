# Production image for the Jetson (linux/arm64).
#
# The Ouster SDK ships manylinux_2_28 aarch64 wheels, which need
# glibc >= 2.28. This image supplies its own (Debian bookworm), so it
# runs on JetPack 4.x hosts as well, whose Ubuntu 18.04 userland is
# too old to pip install the SDK directly.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# The image is built for the field unit; dev runs natively on macOS.
ENV OPENTRAFFIC_PROFILE=prod

WORKDIR /app

# Useful system packages for the Ouster SDK and scientific Python stack.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgl1 \
    libglib2.0-0 \
    iproute2 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY config ./config
COPY api ./api
COPY controllers ./controllers
COPY inspector ./inspector
COPY perception ./perception
COPY recording ./recording
COPY sensors ./sensors
COPY zones ./zones
COPY auth.py health.py settings.py main.py ./

# What the health report calls this build:
#   docker compose build --build-arg VERSION=$(git describe --always --dirty)
ARG VERSION=unknown
ENV OPENTRAFFIC_VERSION=$VERSION

RUN mkdir -p /app/data

# The detector by default; the inspector sidecar runs this same image
# with "python -m inspector" (docker-compose.yml).
CMD ["python", "main.py"]

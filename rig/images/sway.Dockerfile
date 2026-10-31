FROM debian:testing-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      sway swayidle dbus dbus-user-session systemd systemd-sysv libpam-systemd \
      procps python3-venv \
    && python3 -m venv /v && /v/bin/pip install -q dbus-fast numpy opencv-python-headless && useradd -m -u 1000 -s /bin/bash u

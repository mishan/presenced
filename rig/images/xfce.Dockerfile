FROM debian:testing-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      xfce4-session xfwm4 xfce4-settings xfce4-power-manager xfce4-screensaver xfconf \
      xvfb xauth dbus dbus-user-session dbus-x11 systemd systemd-sysv libpam-systemd upower polkitd \
      procps python3-venv \
    && python3 -m venv /v && /v/bin/pip install -q dbus-fast && useradd -m -u 1000 -s /bin/bash u

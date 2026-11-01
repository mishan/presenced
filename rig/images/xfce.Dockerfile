FROM debian:testing-slim@sha256:74c29a19a857bacc7d54bf3e4f2f7a378e6af21886d14af55709d522b2d31224
RUN apt-get update && apt-get install -y --no-install-recommends \
      xfce4-session xfwm4 xfce4-settings xfce4-power-manager xfce4-screensaver xfconf \
      xvfb xauth dbus dbus-user-session dbus-x11 systemd systemd-sysv libpam-systemd upower polkitd \
      procps python3-venv \
    && python3 -m venv /v && /v/bin/pip install -q dbus-fast==5.2.0 numpy==2.5.3 opencv-python-headless==5.0.0.93 && useradd -m -u 1000 -s /bin/bash u

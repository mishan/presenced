FROM debian:testing-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      gnome-shell gnome-session gnome-session-bin gnome-settings-daemon mutter \
      dbus dbus-user-session systemd systemd-sysv libpam-systemd upower polkitd \
      gsettings-desktop-schemas accountsservice libglib2.0-bin procps python3-venv \
    && python3 -m venv /v && /v/bin/pip install -q dbus-fast numpy opencv-python-headless && useradd -m -u 1000 -s /bin/bash u

FROM fedora:44
RUN dnf -y install --setopt=install_weak_deps=False \
      plasma-workspace plasma-desktop kwin kactivitymanagerd powerdevil kscreenlocker \
      systemd systemd-pam dbus-daemon upower polkit procps-ng util-linux mesa-dri-drivers \
      python3 python3-pip \
    && dnf clean all && python3 -m venv /v && /v/bin/pip install -q dbus-fast numpy opencv-python-headless \
    && useradd -m -u 1000 -s /bin/bash u

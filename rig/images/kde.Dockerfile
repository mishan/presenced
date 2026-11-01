FROM fedora:44@sha256:43b29f65a41eb9c35e1cd5323e3bdf3b655c2357a9f4f1ff2f9c2798e5045d80
RUN dnf -y install --setopt=install_weak_deps=False \
      plasma-workspace plasma-desktop kwin kactivitymanagerd powerdevil kscreenlocker \
      systemd systemd-pam dbus-daemon upower polkit procps-ng util-linux mesa-dri-drivers \
      python3 python3-pip \
    && dnf clean all && python3 -m venv /v && /v/bin/pip install -q dbus-fast==5.2.0 numpy==2.5.3 opencv-python-headless==5.0.0.93 \
    && useradd -m -u 1000 -s /bin/bash u

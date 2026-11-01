#!/bin/bash
# A headless GNOME session: gnome-session starts the shell, told not to need a display.
export XDG_CURRENT_DESKTOP=GNOME
if [ -n "${LOCK_AFTER_S:-}" ]; then
    gsettings set org.gnome.desktop.session idle-delay "$LOCK_AFTER_S"
    gsettings set org.gnome.desktop.screensaver lock-delay 0
    gsettings set org.gnome.desktop.screensaver lock-enabled true
fi
if [ -n "${IDLE_ACTION_S:-}" ]; then
    for power in ac battery; do
        gsettings set org.gnome.settings-daemon.plugins.power "sleep-inactive-$power-type" logout
        gsettings set org.gnome.settings-daemon.plugins.power "sleep-inactive-$power-timeout" "$IDLE_ACTION_S"
    done
fi
unit=~/.config/systemd/user/org.gnome.Shell@wayland.service.d
mkdir -p "$unit"
printf '[Service]\nExecStart=\nExecStart=/usr/bin/gnome-shell --headless --wayland --no-x11 --virtual-monitor 1280x720\n' > "$unit/headless.conf"
systemctl --user daemon-reload
systemctl --user import-environment XDG_SESSION_TYPE XDG_CURRENT_DESKTOP XDG_SESSION_ID
gnome-session --session=gnome >/tmp/session.log 2>&1 &
sleep 10
pgrep -x gnome-shell >/dev/null

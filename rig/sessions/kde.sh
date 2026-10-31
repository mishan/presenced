#!/bin/bash
# A headless Plasma: KWin on a virtual output, with the session's services on it.
export XDG_CURRENT_DESKTOP=KDE KDE_SESSION_VERSION=6 KDE_FULL_SESSION=true
export QT_QPA_PLATFORM=wayland WAYLAND_DISPLAY=wayland-rig
kwin_wayland --virtual --socket "$WAYLAND_DISPLAY" --width 1280 --height 720 >/tmp/session.log 2>&1 &
sleep 5
ksmserver >>/tmp/session.log 2>&1 &
/usr/libexec/org_kde_powerdevil >>/tmp/session.log 2>&1 &
sleep 5
pgrep -x kwin_wayland >/dev/null

#!/bin/bash
# Xfce on a virtual X server.
export DISPLAY=:9 XDG_CURRENT_DESKTOP=XFCE
Xvfb "$DISPLAY" -screen 0 1280x720x24 >/tmp/session.log 2>&1 &
sleep 2
xfce4-session >>/tmp/session.log 2>&1 &
sleep 10
xfce4-power-manager >>/tmp/session.log 2>&1 &
xfce4-screensaver >>/tmp/session.log 2>&1 &
sleep 4
pgrep -x xfce4-session >/dev/null

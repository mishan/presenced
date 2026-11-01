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
if [ -n "${LOCK_AFTER_S:-}" ]; then
    set_saver() { xfconf-query -c xfce4-screensaver -p "$1" -n -t "$2" -s "$3"; }
    set_saver /saver/enabled bool true
    set_saver /saver/mode int 0
    set_saver /saver/idle-activation/enabled bool true
    set_saver /saver/idle-activation/delay int "$((LOCK_AFTER_S / 60))"
    set_saver /lock/enabled bool true
    set_saver /lock/saver-activation/enabled bool true
    set_saver /lock/saver-activation/delay int 0
fi
pgrep -x xfce4-session >/dev/null

#!/bin/bash
# A wlroots compositor: no idle inhibition on the bus, which is the case to check.
export WLR_BACKENDS=headless WLR_RENDERER=pixman WLR_LIBINPUT_NO_DEVICES=1 XDG_CURRENT_DESKTOP=sway
sway >/tmp/session.log 2>&1 &
sleep 3
pgrep -x sway >/dev/null

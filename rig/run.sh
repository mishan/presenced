#!/bin/bash
# Check presenced against a real desktop in a container:
#   rig/run.sh gnome|kde|xfce|sway           what the desktop does with our inhibitor
#   rig/run.sh gnome|kde|xfce stay|leave     the whole daemon, with the desktop's idle lock set short
#
# systemd runs as PID 1, without --privileged, so logind and the user manager are
# real; a session is started through PAM, as a display manager would.
set -eu
desktop=$1
check=${2:-contract}
rig=$(cd "$(dirname "$0")" && pwd)
repo=$(dirname "$rig")
image=presenced-rig-$desktop
name=$image-$$
type=wayland
[ "$desktop" = xfce ] && type=x11
lock_after=60  # KDE and Xfce count this in whole minutes
[ "$desktop" = gnome ] && lock_after=30
[ "$check" = leave ] && lock_after=600  # far off: only on_away may lock
if [ "$check" = contract ]; then
    command="/v/bin/python /repo/rig/contract.py $desktop"
    lock_after=
else
    command="/v/bin/python /repo/rig/scenario.py $desktop $check"
fi

docker build -q -f "$rig/images/$desktop.Dockerfile" -t "$image" "$rig/images" >/dev/null
trap 'docker rm -f "$name" >/dev/null 2>&1' EXIT
docker run --name "$name" -d --tmpfs /run --tmpfs /run/lock --cgroupns=private \
    --security-opt seccomp=unconfined --security-opt apparmor=unconfined \
    --cap-add SYS_ADMIN --cap-add SYS_NICE -e container=docker -v "$repo:/repo:ro" \
    "$image" sh -c 'mount -o remount,rw /sys/fs/cgroup && exec /sbin/init' >/dev/null
for _ in $(seq 1 60); do
    docker exec "$name" systemctl is-system-running 2>/dev/null | grep -qE 'running|degraded' && break
    sleep 1
done
docker exec "$name" systemctl mask systemd-suspend.service systemd-hibernate.service >/dev/null 2>&1
if [ "$desktop" = gnome ]; then
    docker exec "$name" cp /repo/rig/standins/gdm.conf /etc/dbus-1/system.d/gdm-rig.conf
    docker exec "$name" busctl call org.freedesktop.DBus / org.freedesktop.DBus ReloadConfig
    docker exec -d "$name" /v/bin/python /repo/rig/standins/gdm.py
    for _ in $(seq 1 20); do
        docker exec "$name" busctl status org.gnome.DisplayManager >/dev/null 2>&1 && break
        sleep 0.5
    done
fi
docker exec "$name" systemd-run --unit=rig --collect -p User=u -p PAMName=login -p WorkingDirectory=/home/u \
    -p Environment=XDG_SESSION_TYPE="$type" -p Environment=XDG_SESSION_CLASS=user \
    -p Environment=XDG_SESSION_DESKTOP="$desktop" -p Environment=PYTHONDONTWRITEBYTECODE=1 \
    -p Environment=LOCK_AFTER_S="$lock_after" \
    -p StandardOutput=file:/tmp/rig.out -p StandardError=file:/tmp/rig.out \
    bash -c "bash /repo/rig/sessions/$desktop.sh && $command > /tmp/rig.json 2>/tmp/rig.err; echo \$? > /tmp/rig.status" >/dev/null
for _ in $(seq 1 420); do
    docker exec "$name" test -e /tmp/rig.status && break
    sleep 1
done
docker exec "$name" cat /tmp/rig.json 2>/dev/null || true
docker exec "$name" sh -c 'tail -n 20 /tmp/rig.err /tmp/session.log 2>/dev/null' >&2 || true
exit "$(docker exec "$name" cat /tmp/rig.status 2>/dev/null || echo 2)"

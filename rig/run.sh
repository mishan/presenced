#!/bin/bash
# Run the contract against a real desktop in a container: rig/run.sh gnome|kde|xfce|sway
#
# systemd runs as PID 1, without --privileged, so logind and the user manager are
# real; a session is started through PAM, as a display manager would.
set -eu
desktop=$1
rig=$(cd "$(dirname "$0")" && pwd)
repo=$(dirname "$rig")
image=presenced-rig-$desktop
name=$image-$$
type=wayland
[ "$desktop" = xfce ] && type=x11

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
docker exec "$name" systemd-run --unit=rig --collect -p User=u -p PAMName=login -p WorkingDirectory=/home/u \
    -p Environment=XDG_SESSION_TYPE="$type" -p Environment=XDG_SESSION_CLASS=user \
    -p Environment=XDG_SESSION_DESKTOP="$desktop" -p Environment=PYTHONDONTWRITEBYTECODE=1 \
    -p StandardOutput=file:/tmp/rig.out -p StandardError=file:/tmp/rig.out \
    bash -c "bash /repo/rig/sessions/$desktop.sh && /v/bin/python /repo/rig/contract.py $desktop > /tmp/rig.json 2>/tmp/rig.err; echo \$? > /tmp/rig.status" >/dev/null
for _ in $(seq 1 240); do
    docker exec "$name" test -e /tmp/rig.status && break
    sleep 1
done
docker exec "$name" cat /tmp/rig.json 2>/dev/null || true
docker exec "$name" sh -c 'tail -n 20 /tmp/rig.err /tmp/session.log 2>/dev/null' >&2 || true
exit "$(docker exec "$name" cat /tmp/rig.status 2>/dev/null || echo 2)"

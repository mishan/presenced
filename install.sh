#!/usr/bin/env bash
# Install presenced for the current user. The program goes in its own virtual
# environment under ~/.local/share/presenced, its commands are linked into
# ~/.local/bin, and it runs as a systemd user service. Nothing here needs root;
# if system packages are missing, it says which, and the command to install
# them on your distribution.
#
#   ./install.sh                 install, or upgrade an existing install
#   ./install.sh --uninstall     remove it, keeping your settings
#   ./install.sh --uninstall --purge   remove settings and the face model too
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: install.sh [options]

  --lock          lock the screen when you leave (asked if not given)
  --no-lock       do not
  --wake          wake the screen when you come back (asked if not given)
  --no-wake       do not
  --calibrate     fit the sonar to this laptop and room after installing; it
                  plays some beeps and asks you to leave the room for half a minute
  --no-service    do not install or start the systemd user service
  --yes           do not ask anything; take the defaults (lock, no wake)
  --uninstall     remove presenced, keeping your settings
  --purge         with --uninstall, also remove settings and the face model
  -h, --help      show this

Settings already in ~/.config/presenced/config.toml are never overwritten;
--lock and --wake only shape a new file.
EOF
}

data_dir="${XDG_DATA_HOME:-$HOME/.local/share}/presenced"
config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/presenced"
unit_file="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/presenced.service"
venv="$data_dir/venv"
bin_dir="$HOME/.local/bin"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
remote="git+https://github.com/mishan/presenced"

lock="" wake="" calibrate=0 service=1 assume_yes=0 uninstall=0 purge=0
while [ $# -gt 0 ]; do
    case "$1" in
        --lock) lock=1 ;;
        --no-lock) lock=0 ;;
        --wake) wake=1 ;;
        --no-wake) wake=0 ;;
        --calibrate) calibrate=1 ;;
        --no-service) service=0 ;;
        --yes|-y) assume_yes=1 ;;
        --uninstall) uninstall=1 ;;
        --purge) purge=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "install.sh: unknown option $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

say() { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

# ask "question" default(y|n): sets REPLY to 1 or 0
ask() {
    local question=$1 default=$2 answer hint="[y/N]"
    [ "$default" = y ] && hint="[Y/n]"
    if [ "$assume_yes" = 1 ] || [ ! -t 0 ]; then
        answer=$default
    else
        read -r -p "$question $hint " answer || answer=""
        answer=${answer:-$default}
    fi
    case "$answer" in [Yy]*) REPLY=1 ;; *) REPLY=0 ;; esac
}

service_active() { systemctl --user is-active --quiet presenced.service 2>/dev/null; }

uninstall_presenced() {
    if [ -f "$unit_file" ]; then
        systemctl --user disable --now presenced.service 2>/dev/null || true
        rm -f "$unit_file"
        systemctl --user daemon-reload 2>/dev/null || true
        say "removed the systemd user service"
    fi
    for command in presenced presencectl; do
        # only our own links: never someone else's program of the same name
        if [ -L "$bin_dir/$command" ] && [ "$(readlink "$bin_dir/$command")" = "$venv/bin/$command" ]; then
            rm -f "$bin_dir/$command"
        fi
    done
    rm -rf "$venv"
    say "removed the program"
    if [ "$purge" = 1 ]; then
        rm -rf "$data_dir" "$config_dir"
        say "removed $config_dir and the face model"
    else
        say "kept your settings in $config_dir (remove them with --uninstall --purge)"
    fi
}

# The distribution's install command, and its package for each thing needed.
detect_packages() {
    local id="" like=""
    if [ -r /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        id=${ID:-} like=${ID_LIKE:-}
    fi
    case " $id $like " in
        *" debian "*|*" ubuntu "*)
            installer="sudo apt install" pkg_python=python3 pkg_venv=python3-venv pkg_pipewire=pipewire-bin ;;
        *" fedora "*|*" rhel "*)
            installer="sudo dnf install" pkg_python=python3 pkg_venv=python3 pkg_pipewire=pipewire-utils ;;
        *" arch "*)
            installer="sudo pacman -S --needed" pkg_python=python pkg_venv=python pkg_pipewire=pipewire ;;
        *" suse "*|*" opensuse "*)
            installer="sudo zypper install" pkg_python=python3 pkg_venv=python3 pkg_pipewire=pipewire-tools ;;
        *) installer="" ;;
    esac
}

python_ok() { command -v python3 >/dev/null && python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))'; }
venv_ok() { python3 -c 'import venv, ensurepip' 2>/dev/null; }
pipewire_ok() { command -v pw-play >/dev/null && command -v pw-record >/dev/null && command -v pw-dump >/dev/null; }

check_requirements() {
    [ "$(uname -s)" = Linux ] || die "presenced only runs on Linux"
    detect_packages

    if command -v python3 >/dev/null && ! python_ok; then
        die "presenced needs Python 3.12 or newer, and this is $(python3 -V 2>&1). Your distribution's next release, or a Python from python.org, will do."
    fi

    local needed=() missing=()
    command -v python3 >/dev/null || { needed+=("${pkg_python:-}"); missing+=("Python 3.12 or newer"); }
    if command -v python3 >/dev/null && ! venv_ok; then needed+=("${pkg_venv:-}"); missing+=("Python's venv module"); fi
    pipewire_ok || { needed+=("${pkg_pipewire:-}"); missing+=("PipeWire's command-line tools (pw-play, pw-record, pw-dump)"); }
    if [ ${#missing[@]} -gt 0 ]; then
        say "presenced needs some system packages first:"
        printf '  - %s\n' "${missing[@]}"
        command -v v4l2-ctl >/dev/null || needed+=(v4l-utils)  # while you are at it: for the IR camera
        if [ -n "$installer" ]; then
            # Python and its venv module are one package on some distributions
            local packages=() package
            for package in "${needed[@]}"; do
                [[ -z "$package" || " ${packages[*]} " == *" $package "* ]] || packages+=("$package")
            done
            say "To install them: $installer ${packages[*]}"
        else
            say "Install them with your distribution's package manager."
        fi
        say "Then run this script again."
        exit 1
    fi
    if ! command -v v4l2-ctl >/dev/null; then
        warn "v4l2-ctl not found, so the IR camera cannot be used and presenced will rely on the sonar alone"
        [ -n "$installer" ] && warn "to use the camera: $installer v4l-utils"
    fi
    if [ "$service" = 1 ] && ! systemctl --user show-environment >/dev/null 2>&1; then
        warn "no systemd user session found; installing without the service"
        service=0
    fi
}

install_presenced() {
    local source was_running=0
    if [ -f "$here/pyproject.toml" ] && grep -q '^name = "presenced"' "$here/pyproject.toml"; then
        source=$here
    else
        source=$remote
    fi
    service_active && was_running=1

    say "installing presenced from $source"
    mkdir -p "$data_dir" "$bin_dir"
    if [ ! -x "$venv/bin/python" ] || ! "$venv/bin/python" -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; then
        rm -rf "$venv"
        python3 -m venv "$venv"
    fi
    "$venv/bin/pip" install --quiet --disable-pip-version-check --upgrade "$source"
    for command in presenced presencectl; do
        if [ -e "$bin_dir/$command" ] && [ "$(readlink "$bin_dir/$command" 2>/dev/null)" != "$venv/bin/$command" ]; then
            warn "$bin_dir/$command is something else; not replacing it (run $venv/bin/$command instead)"
            continue
        fi
        ln -sfn "$venv/bin/$command" "$bin_dir/$command"
    done
    say "installed $("$venv/bin/presencectl" --version)"

    say "fetching the face detection model"
    "$venv/bin/presencectl" fetch-model >/dev/null

    if [ -f "$config_dir/config.toml" ]; then
        say "keeping your settings in $config_dir/config.toml"
    else
        if [ -z "$lock" ]; then ask "Lock the screen when you leave?" y; lock=$REPLY; fi
        if [ -z "$wake" ]; then
            ask "Wake the screen when you come back? (the camera then also looks while the screen is locked)" n
            wake=$REPLY
        fi
        local flags=()
        [ "$lock" = 1 ] && flags+=(--lock)
        [ "$wake" = 1 ] && flags+=(--wake)
        "$venv/bin/presencectl" init-config "${flags[@]}"
    fi

    if [ "$calibrate" = 1 ]; then
        "$venv/bin/presencectl" calibrate --write
    fi

    if [ "$service" = 1 ]; then
        "$venv/bin/presencectl" install-service --enable >/dev/null
        [ "$was_running" = 1 ] && systemctl --user restart presenced.service
        sleep 2
        if service_active; then
            say "presenced is running, and will start with your desktop session"
        else
            warn "presenced did not start; its log says:"
            journalctl --user -u presenced.service -n 15 --no-pager >&2 || true
        fi
    fi

    case ":$PATH:" in
        *":$bin_dir:"*) ;;
        *) warn "$bin_dir is not on your PATH; add it to use presencectl by name" ;;
    esac
    if sed -n '/^\[wake\]/,/^\[/p' "$config_dir/config.toml" 2>/dev/null | grep -q '^enabled = true' &&
        [ ! -w /dev/uinput ]; then
        warn "waking the screen needs write access to /dev/uinput, which you do not have; see \"Waking the screen\" in the README for a udev rule"
    fi
    say "Done. Try: presencectl status"
    [ "$calibrate" = 1 ] || say "For best results, calibrate once in a quiet room: presencectl calibrate --write"
}

if [ "$uninstall" = 1 ]; then
    uninstall_presenced
else
    check_requirements
    install_presenced
fi

#!/bin/sh
# Install or update VinylCast on the Pi itself and (re)install the systemd unit.
# (Replaces the plan's rsync-from-a-dev-machine step: the Pi is the dev box.)
#
# Usage: scripts/deploy.sh [--no-pull] [--enable] [--led]
#   --no-pull  skip `git pull` (deploy the working tree as it is)
#   --enable   also start the service at boot (systemctl enable)
#   --led      install the status-LED extra (gpiozero + lgpio, compiled: needs swig,
#              python3-dev, liblgpio-dev from apt). Implied when vinylcast.toml has
#              status_led = true.
# A running service is restarted so it picks up the new code.
set -eu

PULL=1
ENABLE=0
LED=0
for arg in "$@"; do
    case "$arg" in
        --no-pull) PULL=0 ;;
        --enable) ENABLE=1 ;;
        --led) LED=1 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

DIR=$(cd "$(dirname "$0")/.." && pwd)
USER_NAME=$(id -un)
cd "$DIR"

if [ "$PULL" = 1 ]; then
    git pull --ff-only
fi

missing=""
command -v ffmpeg >/dev/null 2>&1 || missing="$missing ffmpeg"
command -v arecord >/dev/null 2>&1 || missing="$missing alsa-utils"
if [ -n "$missing" ]; then
    echo "Installing:$missing"
    sudo apt-get install -y $missing
fi

if [ -f vinylcast.toml ] && grep -Eq '^status_led *= *true' vinylcast.toml; then
    LED=1
fi
EXTRAS=dev
if [ "$LED" = 1 ]; then
    led_missing=""
    for pkg in swig python3-dev liblgpio-dev; do
        dpkg -s "$pkg" >/dev/null 2>&1 || led_missing="$led_missing $pkg"
    done
    if [ -n "$led_missing" ]; then
        echo "Installing for the status LED (lgpio is compiled):$led_missing"
        sudo apt-get install -y $led_missing
    fi
    EXTRAS=dev,led
fi

[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -e ".[$EXTRAS]"

if [ ! -f vinylcast.toml ]; then
    cp config.example.toml vinylcast.toml
    echo "Created $DIR/vinylcast.toml from config.example.toml"
fi
if ! grep -q '^cast_target *=' vinylcast.toml; then
    echo "NOTE: set cast_target in $DIR/vinylcast.toml before starting the service" >&2
fi

sed -e "s|@USER@|$USER_NAME|g" -e "s|@DIR@|$DIR|g" deploy/vinylcast.service \
    | sudo tee /etc/systemd/system/vinylcast.service >/dev/null
sudo systemctl daemon-reload
echo "Installed /etc/systemd/system/vinylcast.service (User=$USER_NAME, dir $DIR)"

if [ "$ENABLE" = 1 ]; then
    sudo systemctl enable vinylcast
fi
if systemctl is-active --quiet vinylcast; then
    sudo systemctl restart vinylcast
    echo "Restarted vinylcast"
else
    echo "Start with: sudo systemctl start vinylcast   (logs: journalctl -u vinylcast -f)"
fi

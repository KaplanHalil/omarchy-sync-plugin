#!/usr/bin/env bash
# Regenerate the marketplace preview from a live Omarchy capture.
#
# Usage: preview/build.sh <capture.png>
#
# The capture is a full-screen 1920x1080 shot with the Omarchy Sync panel open,
# so the panel sits around x=1150. Needs: chromium, ImageMagick.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
repo=$(dirname "$here")
work="$here/.work"

capture=${1:?full-screen capture with the Omarchy Sync panel open}

command -v chromium >/dev/null || { echo "chromium is required" >&2; exit 1; }
command -v magick >/dev/null || { echo "ImageMagick (magick) is required" >&2; exit 1; }

mkdir -p "$work"
trap '[[ -f $work/server.pid ]] && kill "$(cat "$work/server.pid")" 2>/dev/null || true' EXIT

if [[ ! -f $work/wallpaper.jpg ]]; then
  background=$(readlink -f "$HOME/.local/state/omarchy/current/background")
  cp "$background" "$work/wallpaper.jpg"
fi

# 460x620 around the bar segment and the panel.
magick "$capture" -crop 460x470+1080+0 +repage "$work/shot.png"
cp "$here/poster.html" "$work/poster.html"

port=${PREVIEW_PORT:-8793}
pushd "$work" >/dev/null
python3 -m http.server "$port" --bind 127.0.0.1 >/dev/null 2>&1 &
echo $! > server.pid
popd >/dev/null
sleep 0.5

cd "$work"
chromium --headless=new --disable-gpu --hide-scrollbars \
  --force-device-scale-factor=2 --window-size=1600,1000 \
  --virtual-time-budget=4000 --screenshot=poster2x.png \
  http://127.0.0.1:"$port"/poster.html >/dev/null 2>&1

magick poster2x.png -resize 1600x1000 -strip -define png:compression-level=9 "$repo/preview.png"
echo "wrote preview.png"

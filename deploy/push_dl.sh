#!/usr/bin/env bash
# Publish release artifacts to the download directory the site serves at /dl/.
#
#   bash deploy/push_dl.sh                 # dry run: stage locally, show what would ship
#   bash deploy/push_dl.sh user@vps        # stage + rsync to $DLDIR (default /srv/alphanode-dl)
#
# Sources are the local build outputs (packaging/dist, packaging/Output) — for Windows/macOS
# download the CI artifacts into packaging/dist first. Missing files are SKIPPED with a
# warning, not fatal: shipping linux-only while CI bakes the rest is a normal state.
# Files land under STABLE names (no version in the URL), so download.html never goes stale;
# the version lives in manifest.json, which the page reads for the "v1.4.3 · 122 MB" lines.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ="$(dirname "$HERE")"
DLDIR="${DLDIR:-/srv/alphanode-dl}"
STAGE="$PROJ/packaging/dist/_dl_stage"
VER="$(sed -n "s/.*'\(.*\)'.*/\1/p" "$PROJ/alphanode/version.py" | head -1)"

# stable-name <- source (first glob match wins; deb glob tolerates the versioned filename)
declare -A SRC=(
  ["AlphaNode-Setup.exe"]="$PROJ/packaging/Output/AlphaNode-Setup.exe $PROJ/packaging/dist/AlphaNode-Setup.exe"
  ["AlphaNode-windows-portable.zip"]="$PROJ/packaging/dist/AlphaNode-windows-portable.zip"
  ["AlphaNode-arm64.dmg"]="$PROJ/packaging/dist/AlphaNode-arm64.dmg"
  ["alphanode_amd64.deb"]="$PROJ/packaging/dist/alphanode_${VER}_amd64.deb"
  ["AlphaNode-x86_64.AppImage"]="$PROJ/packaging/dist/AlphaNode-x86_64.AppImage"
  ["docker-compose.yml"]="$HERE/dl/docker-compose.yml"
  ["AlphaNode-Deck.pdf"]="$PROJ/packaging/dist/AlphaNode-Deck.pdf"
)

rm -rf "$STAGE"; mkdir -p "$STAGE"
manifest="{\"version\": \"$VER\", \"files\": {"
sep=""
for name in "AlphaNode-Setup.exe" "AlphaNode-windows-portable.zip" "AlphaNode-arm64.dmg" \
            "alphanode_amd64.deb" "AlphaNode-x86_64.AppImage" "docker-compose.yml" \
            "AlphaNode-Deck.pdf"; do
  src=""
  for cand in ${SRC[$name]}; do                       # first existing candidate wins
    if [ -f "$cand" ]; then src="$cand"; break; fi
  done
  if [ -n "$src" ]; then
    cp "$src" "$STAGE/$name"
    bytes=$(wc -c < "$STAGE/$name")                   # portable: GNU `stat -c%s` dies on macOS
    mb=$(( (bytes + 524288) / 1048576 ))
    if [ "$mb" -ge 1 ]; then size="${mb} MB"; else size="$(( (bytes+512)/1024 )) KB"; fi
    manifest+="$sep\"$name\": \"$size\""; sep=", "
    printf '  + %-34s %8s\n' "$name" "$size"
  else
    echo "  ! SKIP $name — not built yet (${SRC[$name]})"
  fi
done
manifest+="}}"
printf '%s\n' "$manifest" | python3 -m json.tool > "$STAGE/manifest.json"
echo "  = manifest.json (v$VER)"

if [ $# -lt 1 ]; then
  echo
  echo "dry run — staged in $STAGE; to publish:  bash deploy/push_dl.sh user@vps"
  exit 0
fi
HOST="$1"
ssh "$HOST" "sudo mkdir -p '$DLDIR' && sudo chown \$(id -u):\$(id -g) '$DLDIR'"
rsync -av --progress "$STAGE/" "$HOST:$DLDIR/"
echo "✓ published: https://alphanode.tech/dl/  (site container serves it read-only)"

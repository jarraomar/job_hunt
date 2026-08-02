#!/usr/bin/env bash
# Build Tailwind CSS with the standalone binary -- no node_modules, no npm.
#
# Deliberately a LOCAL step. Spec section 12 rules out a deploy-time JS build,
# which is what keeps the deployment one Python function rather than two
# runtimes. The output is committed and CI checks it is current.
set -euo pipefail

VERSION="${JOBHUNT_TAILWIND_VERSION:-v4.1.14}"
BIN="${JOBHUNT_TAILWIND_BIN:-$HOME/.local/bin/tailwindcss}"

if [ ! -x "$BIN" ]; then
  case "$(uname -sm)" in
    "Darwin arm64")  ASSET="tailwindcss-macos-arm64" ;;
    "Darwin x86_64") ASSET="tailwindcss-macos-x64" ;;
    "Linux x86_64")  ASSET="tailwindcss-linux-x64" ;;
    "Linux aarch64") ASSET="tailwindcss-linux-arm64" ;;
    *) echo "no standalone Tailwind build for $(uname -sm)" >&2; exit 1 ;;
  esac
  echo "fetching Tailwind ${VERSION} (${ASSET})"
  mkdir -p "$(dirname "$BIN")"
  curl -fsSL "https://github.com/tailwindlabs/tailwindcss/releases/download/${VERSION}/${ASSET}" -o "$BIN"
  chmod +x "$BIN"
fi

cd "$(dirname "$0")/.."
"$BIN" -i web/templates/input.css -o web/static/app.css --minify
echo "wrote web/static/app.css ($(wc -c < web/static/app.css) bytes)"

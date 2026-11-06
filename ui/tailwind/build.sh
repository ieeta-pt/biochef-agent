#!/usr/bin/env bash
# Builds ui/static/app.css with the pinned Tailwind CLI. No Node, no npm: the
# standalone binary comes from the GitHub release and is checked against
# SHA256SUMS before it runs, here and in CI. The built file is committed, so the
# agent itself never needs this.
set -euo pipefail

VERSION=4.3.3
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
CACHE="$ROOT/.cache/tailwind-$VERSION"

case "$(uname -s)-$(uname -m)" in
    Linux-x86_64) BINARY=tailwindcss-linux-x64 ;;
    Darwin-arm64) BINARY=tailwindcss-macos-arm64 ;;
    *)
        echo "No pinned Tailwind CLI for $(uname -s) $(uname -m); see $HERE/SHA256SUMS." >&2
        exit 1
        ;;
esac

digest() {
    if command -v sha256sum >/dev/null; then
        sha256sum "$1" | cut -d' ' -f1
    else
        shasum -a 256 "$1" | cut -d' ' -f1
    fi
}

EXPECTED="$(grep " $BINARY\$" "$HERE/SHA256SUMS" | cut -d' ' -f1)"
mkdir -p "$CACHE"
if [ ! -f "$CACHE/$BINARY" ]; then
    curl --fail --silent --show-error --location --proto '=https' \
        --output "$CACHE/$BINARY.part" \
        "https://github.com/tailwindlabs/tailwindcss/releases/download/v$VERSION/$BINARY"
    mv "$CACHE/$BINARY.part" "$CACHE/$BINARY"
fi

# Checked on every build, not only after a download: a cached binary is a binary
# about to be run.
if [ "$(digest "$CACHE/$BINARY")" != "$EXPECTED" ]; then
    echo "$CACHE/$BINARY does not match SHA256SUMS; not running it." >&2
    exit 1
fi
chmod u+x "$CACHE/$BINARY"

cd "$ROOT"
"$CACHE/$BINARY" --input ui/tailwind/input.css --output ui/static/app.css --minify

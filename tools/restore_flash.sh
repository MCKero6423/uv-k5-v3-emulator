#!/usr/bin/env bash
# Restore assets/flash.img to its pristine, never-booted state.
#
# The emulator writes to the flash image, so a session can leave settings, edited
# frequencies, or a corrupted EEPROM behind. assets/pristine/ holds a checksummed
# copy of the image as first generated, so there is always a known-good state to
# come back to.
#
# Usage:
#   tools/restore_flash.sh            # restore, backing up the current image
#   tools/restore_flash.sh --verify   # only check the pristine copy is intact
#   tools/restore_flash.sh --diff     # show whether the live image has changed
set -euo pipefail

SIM="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIVE="$SIM/assets/flash.img"
PRISTINE_GZ="$SIM/assets/pristine/flash-pristine.img.gz"
SUMS="$SIM/assets/pristine/flash-pristine.img.sha256"

want_plain() { awk '$2 == "flash-pristine.img" {print $1}' "$SUMS"; }
want_gz()    { awk '$2 == "flash-pristine.img.gz" {print $1}' "$SUMS"; }

verify_pristine() {
    [ -f "$PRISTINE_GZ" ] || { echo "missing $PRISTINE_GZ" >&2; exit 1; }
    local got_gz got_plain
    got_gz=$(sha256sum "$PRISTINE_GZ" | awk '{print $1}')
    if [ "$got_gz" != "$(want_gz)" ]; then
        echo "pristine archive is CORRUPT" >&2
        echo "  expected $(want_gz)" >&2
        echo "  actual   $got_gz" >&2
        exit 1
    fi
    got_plain=$(gzip -dc "$PRISTINE_GZ" | sha256sum | awk '{print $1}')
    if [ "$got_plain" != "$(want_plain)" ]; then
        echo "pristine contents do not match their checksum" >&2
        exit 1
    fi
    echo "pristine copy verified ($(want_plain))"
}

case "${1:-restore}" in
--verify)
    verify_pristine
    ;;
--diff)
    verify_pristine
    if [ ! -f "$LIVE" ]; then
        echo "no live image at $LIVE"
        exit 0
    fi
    live=$(sha256sum "$LIVE" | awk '{print $1}')
    if [ "$live" = "$(want_plain)" ]; then
        echo "live image is unchanged from pristine"
    else
        echo "live image HAS CHANGED from pristine"
        echo "  pristine $(want_plain)"
        echo "  live     $live"
        gzip -dc "$PRISTINE_GZ" > /tmp/.pristine.$$
        echo "  differing bytes: $(cmp -l /tmp/.pristine.$$ "$LIVE" 2>/dev/null | wc -l)"
        rm -f /tmp/.pristine.$$
    fi
    ;;
restore)
    verify_pristine
    if [ -f "$LIVE" ]; then
        backup="$LIVE.bak-$(date +%Y%m%d-%H%M%S)"
        cp -a "$LIVE" "$backup"
        echo "current image saved to $(basename "$backup")"
    fi
    gzip -dc "$PRISTINE_GZ" > "$LIVE"
    got=$(sha256sum "$LIVE" | awk '{print $1}')
    [ "$got" = "$(want_plain)" ] || { echo "restore verification FAILED" >&2; exit 1; }
    echo "restored $LIVE to pristine state"
    echo "note: the emulator reads the image at boot, so power-cycle to pick it up"
    ;;
*)
    echo "usage: $0 [restore|--verify|--diff]" >&2
    exit 2
    ;;
esac

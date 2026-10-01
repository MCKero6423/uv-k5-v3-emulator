#!/usr/bin/env bash
# Where the firmware is, for the ad-hoc probe scripts.
#
# Every one of them used to name the same hardcoded path -- one developer's CW build --
# so none of them ran on a machine that did not have that file. The order here matches
# tools/uvk5_testenv.py, so the scripts and the tests agree about what is available.
#
#   ELF, UVK5_FIRMWARE, UVK5_MULTIBOOT_IMAGE   the environment wins
#   assets/firmware/*, work/*                  then whatever the checkout has
#
# Returns non-zero when there is nothing, so a caller can skip with a reason rather
# than run against a path that does not exist.
uvk5_find_elf() {
    local var value
    for var in ELF UVK5_FIRMWARE UVK5_MULTIBOOT_IMAGE; do
        eval "value=\${$var:-}"
        if [ -n "$value" ] && [ -e "$value" ]; then
            printf '%s' "$value"
            return 0
        fi
    done
    local root cand
    root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
    for cand in "$root"/assets/firmware/*.elf "$root"/assets/firmware/*.bin \
                "$root"/work/*.elf "$root"/work/*.bin; do
        if [ -e "$cand" ]; then
            printf '%s' "$cand"
            return 0
        fi
    done
    return 1
}

#!/usr/bin/env bash
# The test runner must actually notice a failing test.
#
# This is not a hypothetical worry. The first version of run_tests.sh used
#
#     if "$@" 2>&1 | sed 's/^/    /'; then
#
# which checks *sed's* exit status, not the test's. sed almost always succeeds, so every
# test would have been counted as passing and the runner would have reported a clean
# suite no matter what broke. A test runner that cannot fail is worse than none, because
# it is trusted.
#
# Checked here:
#   1. a failing command is counted as a failure and named
#   2. a passing command is counted as a pass
#   3. the runner's own exit status is non-zero when something failed
#   4. binary noise in a test's output does not break the accounting
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
RUNNER="$HERE/run_tests.sh"

[ -f "$RUNNER" ] || { echo "FAIL  run_tests.sh not found"; exit 1; }

failures=0

# Extract the run() helper and exercise it in isolation, so this test does not have to
# boot an emulator to check the accounting logic.
harness=$(mktemp /tmp/run-harness-XXXX.sh)
trap 'rm -f "$harness" /tmp/rt-good /tmp/rt-bad /tmp/rt-noisy' EXIT

sed -n '/^run() {/,/^}/p' "$RUNNER" > "$harness"
if ! grep -q PIPESTATUS "$harness"; then
    echo "FAIL  run() does not use PIPESTATUS; it is checking the wrong exit status"
    echo "      (a piped command's \$? is the last stage, so every test would 'pass')"
    exit 1
fi
echo "PASS  run() checks the test's status, not the pipeline's last stage"

cat >> "$harness" <<'EOF'
pass=0; fail=0; failed_names=""
run "good"  /tmp/rt-good
run "bad"   /tmp/rt-bad
run "noisy" /tmp/rt-noisy
echo "RESULT pass=$pass fail=$fail failed:$failed_names"
EOF

printf '#!/bin/sh\necho fine\n' > /tmp/rt-good
printf '#!/bin/sh\necho broken\nexit 1\n' > /tmp/rt-bad
# Emits raw bytes, as gdb-driven tests can. Left unfiltered these make the combined
# output a "binary file" to grep, which silently swallows the summary line.
printf '#!/bin/sh\nprintf "noise\\001\\002\\003\\n"\nexit 0\n' > /tmp/rt-noisy
chmod +x /tmp/rt-good /tmp/rt-bad /tmp/rt-noisy

out=$(bash "$harness" 2>&1)
result=$(printf '%s\n' "$out" | grep '^RESULT' || true)

echo "  $result"

case "$result" in
    *"pass=2"*) echo "PASS  both passing tests counted" ;;
    *) echo "FAIL  expected pass=2"; failures=$((failures + 1)) ;;
esac

case "$result" in
    *"fail=1"*) echo "PASS  the failing test was counted" ;;
    *) echo "FAIL  expected fail=1; a failing test went unnoticed"
       failures=$((failures + 1)) ;;
esac

case "$result" in
    *"failed: bad"*) echo "PASS  the failing test was named" ;;
    *) echo "FAIL  the failing test was not named"; failures=$((failures + 1)) ;;
esac

# The runner must exit non-zero on failure, or CI and shell && chains ignore it.
if grep -q 'exit 1' "$RUNNER"; then
    echo "PASS  the runner exits non-zero when tests fail"
else
    echo "FAIL  the runner never exits non-zero"
    failures=$((failures + 1))
fi

if [ "$failures" = "0" ]; then
    echo
    echo "the test runner reports failures correctly"
    exit 0
fi
exit 1

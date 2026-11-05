#!/bin/sh
# Usage: simulator/entries.sh OUT
# Run each entry command twice on a fresh simulated Echo, once to act and once
# to find the state it left, and check what each run says. Leaves OUT.<case>.log
# for every run. Exits non-zero if any check fails.
set -u
here=$(cd "$(dirname "$0")" && pwd)
out=$1
cache=${FIREBREAK_SIM_CACHE:-$HOME/.cache/firebreak-sim-cache}
work=${FIREBREAK_SIM:-$HOME/.cache/firebreak-sim}
python=$work/venv/bin/python
FIREBREAK_SIM_SPEED=${FIREBREAK_SIM_SPEED:-20}
export FIREBREAK_SIM_SPEED
PATH="$here/bin:$PATH"
export PATH
plugger=''
passed=0 failed=0
finish() {
    [ -n "$plugger" ] && kill "$plugger" 2>/dev/null
    python3 "$here/dot.py" destroy >/dev/null 2>&1
}
trap finish EXIT
trap 'exit 130' HUP INT QUIT TERM

firebreak() {
    log=$out.$1.log
    secs=$2
    shift 2
    : > "$log"
    PYTHONUNBUFFERED=1 XDG_CACHE_HOME=$cache PYTHONPATH="$here/.." \
        timeout "$secs" "$python" -m firebreak "$@" --no-update-check </dev/null \
        > "$log" 2>&1
}

run() {
    label=$1 secs=$2 want=$3
    shift 3
    firebreak "$label" "$secs" "$@"
    if grep -qF "$want" "$out.$label.log"; then
        passed=$((passed + 1))
        printf '    ok    %s\n' "$want"
    else
        failed=$((failed + 1))
        printf '    FAIL  wanted %s\n          got    %s\n' "$want" \
            "$(grep -m1 -oE '(✅ The|ERROR:|Start the|Short the).*' \
                "$out.$label.log" | cut -c1-76)"
    fi
}

check() {
    name=$1 device=$2 start=$3 prep=$4 secs=$5 want1=$6 want2=$7
    shift 8
    echo "== $name [$device, $start${prep:+, after $prep}]"
    python3 "$here/dot.py" destroy >/dev/null 2>&1
    python3 "$here/dot.py" create --device "$device" --runner $$ --start "$start" \
        >/dev/null || { echo "    create failed"; failed=$((failed + 2)); return; }
    if [ "$start" = no-light ] || [ "$device" = donut ]; then
        (
            trap 'exit 0' TERM
            until grep -qE "test point and plug it in|start it in its bootrom" "$out.$name-1.log" 2>/dev/null
            do
                sleep 0.2
            done
            "$python" "$here/dot.py" plug >/dev/null
        ) &
        plugger=$!
    fi
    # shellcheck disable=SC2086
    [ -n "$prep" ] && firebreak "$name-0" 600 $prep
    run "$name-1" "$secs" "$want1" "$@"
    if [ -n "$plugger" ]; then
        kill "$plugger" 2>/dev/null
        wait "$plugger" 2>/dev/null
        plugger=''
    fi
    run "$name-2" "$secs" "$want2" "$@"
}

A="is in unlocked fastboot."
T="is in TWRP."
H="waits in its bootrom."
AH="already waits in its bootrom."
LOCKED="is locked, so reaching a TWRP unlocks it"
REFUSED="locked, and a locked bootloader"
NO_WAY="reads as stock-booted, which nothing here"
GESTURE="Start the Echo Dot (2nd Generation)'s fastboot"
RUNS="runs its firmware, read as stock-booted."
NO_CHAIN="has no bootrom chain for the Echo Dot (3rd Generation)"
NO_BRICK="has no fastbrick for the Echo Dot (3rd Generation)"
NOT_AMONET="Echo Dot (3rd Generation), which amonet does not unlock"
D_GESTURE="Start the Echo Dot (3rd Generation)'s fastboot"
CYCLE="waits in its bootrom, and nothing here restarts it"

check fb-twrp       biscuit twrp         ""         600 "$A" "$A" -- fastboot
check fb-stockfb    biscuit fastboot     ""         600 "is in stock fastboot." \
    "is in stock fastboot." -- fastboot
check twrp-twrp     biscuit twrp         ""         600 "$T" "$T" -- twrp
check twrp-stockfb  biscuit fastboot     ""         600 "$LOCKED" "$LOCKED" -- twrp
check twrpf-stockfb biscuit fastboot     ""         600 "$T" "$T" -- twrp --force
check twrp-amonetfb biscuit twrp         "fastboot" 600 "$T" "$T" -- twrp
check brom-twrp     biscuit twrp         ""         600 "$H" "$AH" -- bootrom --force
check brom-stockfb  biscuit fastboot     ""         600 "$REFUSED" "$REFUSED" \
    -- bootrom --force
check brom-amonetfb biscuit twrp         "fastboot" 600 "$H" "$AH" -- bootrom --force
check short-dark    biscuit no-light     ""         600 "$H" "$AH" \
    -- short --device biscuit
check brom-booted   biscuit stock-booted ""         600 "$NO_WAY" "$NO_WAY" \
    -- bootrom --force
check fb-booted     biscuit stock-booted ""          60 "$GESTURE" "$GESTURE" \
    -- fastboot
check twrp-booted   biscuit stock-booted ""          60 "$GESTURE" "$GESTURE" -- twrp
check rb-twrp       biscuit twrp         ""         600 "$RUNS" "$RUNS" -- reboot
check rb-stockfb    biscuit fastboot     ""         600 "$RUNS" "$RUNS" -- reboot
check rb-amonetfb   biscuit twrp         "fastboot" 600 "$RUNS" "$RUNS" -- reboot
check rb-brom       biscuit twrp         "bootrom --force" 600 "$CYCLE" "$CYCLE" \
    -- reboot
check rb-booted     biscuit stock-booted ""         600 "$RUNS" "$RUNS" -- reboot
check r-fb-twrp     radar   twrp         ""         600 "$A" "$A" -- fastboot
check r-twrp-twrp   radar   twrp         ""         600 "$T" "$T" -- twrp
check r-brom-twrp   radar   twrp         ""         600 "$H" "$AH" -- bootrom --force
check r-short-dark  radar   no-light     ""         600 "$H" "$AH" \
    -- short --device radar
check r-rb-twrp     radar   twrp         ""         600 "$RUNS" "$RUNS" -- reboot
check d-fb-twrp     donut   twrp         ""         600 "$A" "$A" -- fastboot
check d-fb-stockfb  donut   fastboot     ""         600 "is in stock fastboot." \
    "is in stock fastboot." -- fastboot
check d-twrp-twrp   donut   twrp         ""         600 "$T" "$T" -- twrp
check d-twrp-amonetfb donut twrp         "fastboot" 600 "$T" "$T" -- twrp
check d-twrp-stockfb donut  fastboot     ""         600 "$NO_BRICK" "$NO_BRICK" \
    -- twrp --force
check d-brom-twrp   donut   twrp         ""         600 "$NO_CHAIN" "$NO_CHAIN" \
    -- bootrom --force
check d-short       donut   twrp         ""         600 "$NOT_AMONET" "$NOT_AMONET" \
    -- short --device donut
check d-fb-booted   donut   stock-booted ""          60 "$D_GESTURE" "$D_GESTURE" \
    -- fastboot
check d-rb-twrp     donut   twrp         ""         600 "$RUNS" "$RUNS" -- reboot
check d-rb-stockfb  donut   fastboot     ""         600 "$RUNS" "$RUNS" -- reboot
check d-rb-booted   donut   stock-booted ""         600 "$RUNS" "$RUNS" -- reboot

echo
echo "passed $passed, failed $failed"
[ "$failed" -eq 0 ]

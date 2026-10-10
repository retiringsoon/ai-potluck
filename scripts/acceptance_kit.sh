#!/bin/sh
# Timing and record-keeping kit for the first-trial acceptance table (§5.1 C).
#
# Why this is a script and not a spreadsheet: the acceptance criteria in the
# plan are *measurements* — "from starting the install to seeing the first valid
# answer, target under 5 minutes", install success rate, first-answer median,
# how many times a human had to help. Measurements taken by hand after the fact
# are guesses. This kit takes the two timestamps while the trial is running and
# keeps them in an append-only JSONL file, so `record` and `stats` cannot
# disagree with what actually happened.
#
# What it deliberately does NOT do: run the install, run the trial, or decide
# whether T01–T06 passed. It is a stopwatch and a ledger, nothing more. The real
# user test is the task of the person running it, not of this script.
#
# Desensitisation: every string this script persists or prints as a table row
# passes through assert_clean(), which refuses anything that looks like a
# bearer token, a private key block, an upstream API key or a cloud access key.
# The check is fail-closed on purpose — a false positive costs one retry, a
# leaked key costs the identity. `selftest` proves the guard rejects them.
#
# Usage:
#   scripts/acceptance_kit.sh start  [--todo T01] [--note "..."]
#   scripts/acceptance_kit.sh stop   [--assisted yes|no] [--result success|failed|partial]
#   scripts/acceptance_kit.sh record [--todo T01] [--result ...] [--assisted ...]
#                                    [--log PATH] [--next "..."]
#   scripts/acceptance_kit.sh stats
#   scripts/acceptance_kit.sh selftest        # proves the redaction guard works
#   scripts/acceptance_kit.sh where           # prints the log paths in use
#
# Environment:
#   AIPOTLUCK_HOME             base dir for config/keys; default ~/.aipotluck
#   AIPOTLUCK_ACCEPTANCE_LOG   JSONL path; default $AIPOTLUCK_HOME/acceptance.jsonl
#
# Exit codes: 0 ok, 1 usage/environment problem, 2 no run in progress,
# 3 a string failed the desensitisation guard.

set -eu

BASE=${AIPOTLUCK_HOME:-"$HOME/.aipotluck"}
LOG=${AIPOTLUCK_ACCEPTANCE_LOG:-"$BASE/acceptance.jsonl"}
STATE="$BASE/acceptance-current"
# shellcheck disable=SC1007  # the leading assignment is how CDPATH is neutralised
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

# ---------------------------------------------------------------- redaction --

# Fail-closed denylist. Kept in one place so `selftest` and the live paths can
# never drift apart: both call assert_clean(), and selftest feeds this same
# variable the strings that must be rejected.
#
# The pattern begins with `-`, so every grep that consumes it must be given `-e`.
# Without that, BSD, GNU and toybox grep all read the pattern as a list of
# options and match nothing — which is exactly how this guard silently stopped
# guarding the first time it was run. `selftest` is what caught that.
SECRET_PATTERNS='-----BEGIN|PRIVATE KEY|Bearer |authorization[:=]|api[-_]?key|access[-_]?key|AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{8}|ghp_[A-Za-z0-9]{20}|xox[baprs]-[A-Za-z0-9-]{10}'

die() { printf 'acceptance_kit.sh: %s\n' "$1" >&2; exit "${2:-1}"; }

# Reads the candidate string on stdin; exits 3 if it carries a secret.
assert_clean() {
    candidate=$(cat)
    # Pass 1: unambiguous shapes (key blocks, auth headers, provider key prefixes).
    if printf '%s' "$candidate" | grep -Eqi -e "$SECRET_PATTERNS"; then
        printf 'acceptance_kit.sh: refusing to persist a value that looks secret\n' >&2
        printf 'acceptance_kit.sh: strip tokens, private keys and upstream keys before recording\n' >&2
        exit 3
    fi
    # Pass 2: an ordinary word (`token`, `secret`, …) directly assigned a value.
    # The `:`/`=` separator is required so a note like "token exchange failed"
    # stays allowed while "token=abc123" does not.
    if printf '%s' "$candidate" | grep -Eqi -e '(token|secret|password|passwd)[[:space:]]*[:=]'; then
        printf 'acceptance_kit.sh: refusing to persist a value that looks secret\n' >&2
        printf 'acceptance_kit.sh: strip tokens, private keys and upstream keys before recording\n' >&2
        exit 3
    fi
    printf '%s' "$candidate"
}

# ---------------------------------------------------------------- helpers ----

# Minimal JSON string escaping for the values we write. Control characters that
# survive sh's argument passing are only tab and newline, so those two plus the
# backslash and the quote are all that need handling.
json_str() {
    printf '%s' "$1" | tr -d '\n\r' \
        | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/	/\\t/g'
}

now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }
now_epoch() { date -u +%s; }

package_version() {
    if [ -f "$ROOT/pyproject.toml" ]; then
        v=$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/pyproject.toml" | head -1)
        [ -n "$v" ] && { printf '%s' "$v"; return; }
    fi
    if command -v potluck >/dev/null 2>&1; then
        # `potluck version` prints "<package> <protocol>"; the package is first.
        potluck version 2>/dev/null | awk 'NR==1 {print $1}' || true
    fi
}

# Anonymous environment label: OS family, architecture, Python version. No
# hostname, no username, no path — the plan asks for "user or environment (anonymous)".
env_label() {
    os=$(uname -s 2>/dev/null || printf 'unknown')
    case "$os" in
        Darwin) os=macOS ;;
    esac
    arch=$(uname -m 2>/dev/null || printf 'unknown')
    py=''
    if command -v python3 >/dev/null 2>&1; then
        py=$(python3 -c 'import sys; print("py%d.%d" % sys.version_info[:2])' 2>/dev/null || true)
    fi
    [ -n "$py" ] && printf '%s-%s·%s' "$os" "$arch" "$py" || printf '%s-%s' "$os" "$arch"
}

scenario_label() {
    case "$1" in
        T01) printf 'first success' ;;
        T02) printf 'next-day reuse' ;;
        T03) printf 'continuous use and interruption' ;;
        T04) printf 'contributor operations' ;;
        T05) printf 'real feedback' ;;
        T06) printf 'later compatibility entry point' ;;
        *)   printf '%s' "$1" ;;
    esac
}

result_label() {
    case "$1" in
        success) printf 'success' ;;
        partial) printf 'partially done' ;;
        failed)  printf 'failure' ;;
        *)       printf '%s' "$1" ;;
    esac
}

assist_label() {
    case "$1" in
        yes) printf 'assistance needed' ;;
        no)  printf 'no assistance needed' ;;
        *)   printf '%s' "$1" ;;
    esac
}

# Append one event line. Every field value is redaction-checked first.
log_event() {
    line=$1
    clean=$(printf '%s' "$line" | assert_clean)
    mkdir -p "$(dirname -- "$LOG")"
    printf '%s\n' "$clean" >> "$LOG"
}

# ---------------------------------------------------------------- commands ---

cmd_start() {
    todo=T01
    note=''
    while [ $# -gt 0 ]; do
        case "$1" in
            --todo) todo=${2:?--todo needs a value}; shift 2 ;;
            --note) note=${2:?--note needs a value}; shift 2 ;;
            *) die "unknown argument $1" ;;
        esac
    done
    [ -e "$STATE" ] && die "a run is already in progress (see $STATE); run 'stop' first" 2

    started=$(now_epoch)
    run_id="$(date -u +%Y%m%dT%H%M%S)-$$"
    version=$(package_version); version=${version:-unknown}
    env=$(env_label)

    line=$(printf '{"event":"install_start","run_id":"%s","at":"%s","epoch":%s,"version":"%s","env":"%s","todo":"%s","note":"%s"}' \
        "$(json_str "$run_id")" "$(now_iso)" "$started" \
        "$(json_str "$version")" "$(json_str "$env")" \
        "$(json_str "$todo")" "$(json_str "$note")")
    log_event "$line"

    mkdir -p "$BASE"
    printf '%s\n' "$run_id" > "$STATE"
    printf 'install_start recorded (run %s, %s, %s)\n' "$run_id" "$version" "$env"
    printf 'now install and get a first answer, then run: scripts/acceptance_kit.sh stop\n'
}

cmd_stop() {
    assisted=no
    result=success
    note=''
    while [ $# -gt 0 ]; do
        case "$1" in
            --assisted) assisted=${2:?--assisted needs yes|no}; shift 2 ;;
            --result) result=${2:?--result needs a value}; shift 2 ;;
            --note) note=${2:?--note needs a value}; shift 2 ;;
            *) die "unknown argument $1" ;;
        esac
    done
    [ -f "$STATE" ] || die 'no run in progress; run start first' 2

    run_id=$(head -1 "$STATE")
    start_epoch=$(grep -m1 "\"run_id\":\"$run_id\"" "$LOG" \
        | sed -n 's/.*"epoch":\([0-9]*\).*/\1/p')
    [ -n "$start_epoch" ] || die "no install_start found for run $run_id" 2

    stopped=$(now_epoch)
    elapsed=$((stopped - start_epoch))
    [ "$elapsed" -ge 0 ] || die 'clock went backwards; refusing to record a negative duration' 2

    version=$(package_version); version=${version:-unknown}
    env=$(env_label)
    line=$(printf '{"event":"first_answer","run_id":"%s","at":"%s","epoch":%s,"elapsed_seconds":%s,"version":"%s","env":"%s","result":"%s","assisted":"%s","note":"%s"}' \
        "$(json_str "$run_id")" "$(now_iso)" "$stopped" "$elapsed" \
        "$(json_str "$version")" "$(json_str "$env")" \
        "$(json_str "$result")" "$(json_str "$assisted")" "$(json_str "$note")")
    log_event "$line"

    rm -f "$STATE"
    printf 'first_answer recorded (run %s, %ss, %s)\n' "$run_id" "$elapsed" "$(assist_label "$assisted")"
    printf 'now paste this into the acceptance table: scripts/acceptance_kit.sh record\n'
}

cmd_record() {
    todo=''
    result=''
    assisted=''
    logdoc='—'
    nextstep='to be filled in'
    while [ $# -gt 0 ]; do
        case "$1" in
            --todo) todo=${2:?--todo needs a value}; shift 2 ;;
            --result) result=${2:?--result needs a value}; shift 2 ;;
            --assisted) assisted=${2:?--assisted needs yes|no}; shift 2 ;;
            --log) logdoc=${2:?--log needs a path}; shift 2 ;;
            --next) nextstep=${2:?--next needs a value}; shift 2 ;;
            *) die "unknown argument $1" ;;
        esac
    done

    [ -f "$LOG" ] || die "no acceptance log at $LOG; run start/stop first" 2

    # The last completed run is what a person means by "this trial".
    run_id=$(grep '"event":"first_answer"' "$LOG" | tail -1 \
        | sed -n 's/.*"run_id":"\([^"]*\)".*/\1/p' | head -1)
    [ -n "$run_id" ] || die 'no completed run in the log; run start then stop first' 2

    start_line=$(grep "\"event\":\"install_start\"" "$LOG" | grep -m1 "\"run_id\":\"$run_id\"")
    stop_line=$(grep "\"event\":\"first_answer\"" "$LOG" | grep -m1 "\"run_id\":\"$run_id\"")
    [ -n "$start_line" ] || die "run $run_id has no install_start line" 2

    version=$(printf '%s' "$stop_line" | sed -n 's/.*"version":"\([^"]*\)".*/\1/p')
    env=$(printf '%s' "$stop_line" | sed -n 's/.*"env":"\([^"]*\)".*/\1/p')
    elapsed=$(printf '%s' "$stop_line" | sed -n 's/.*"elapsed_seconds":\([0-9]*\).*/\1/p')
    [ -n "$result" ] || result=$(printf '%s' "$stop_line" | sed -n 's/.*"result":"\([^"]*\)".*/\1/p')
    [ -n "$assisted" ] || assisted=$(printf '%s' "$stop_line" | sed -n 's/.*"assisted":"\([^"]*\)".*/\1/p')
    [ -n "$todo" ] || todo=$(printf '%s' "$start_line" | sed -n 's/.*"todo":"\([^"]*\)".*/\1/p')
    [ -n "$todo" ] || todo=T01

    date_part=$(printf '%s' "$stop_line" | sed -n 's/.*"at":"\([0-9-]*\)T.*/\1/p')
    row=$(printf '| %s / %s | %s | %s·%s | %s | %ss / %s | %s | %s |' \
        "$date_part" "$version" "$env" "$todo" "$(scenario_label "$todo")" \
        "$(result_label "$result")" "$elapsed" "$(assist_label "$assisted")" \
        "$logdoc" "$nextstep")

    clean=$(printf '%s' "$row" | assert_clean)
    printf '%s\n' "$clean"
    printf '\n# paste the line above into docs/acceptance-record.md\n' >&2
}

cmd_stats() {
    [ -f "$LOG" ] || die "no acceptance log at $LOG; run start/stop first" 2

    started=$(grep -c '"event":"install_start"' "$LOG" || true)
    finished=$(grep -c '"event":"first_answer"' "$LOG" || true)
    assisted=$(grep '"event":"first_answer"' "$LOG" | grep -c '"assisted":"yes"' || true)

    if [ "$started" -eq 0 ]; then
        printf 'runs=0 (no install_start recorded yet)\n'
        return 0
    fi
    rate=$(awk -v f="$finished" -v s="$started" 'BEGIN { printf "%.1f", (s > 0 ? f * 100.0 / s : 0) }')

    tmp=$(mktemp)
    trap 'rm -f "$tmp"' EXIT
    grep '"event":"first_answer"' "$LOG" \
        | sed -n 's/.*"elapsed_seconds":\([0-9]*\).*/\1/p' | sort -n > "$tmp"
    median=$(awk '
        { v[NR] = $1 }
        END {
            if (NR == 0) { print "n/a"; exit }
            if (NR % 2) { print v[(NR + 1) / 2] }
            else { printf "%.1f", (v[NR / 2] + v[NR / 2 + 1]) / 2 }
        }' "$tmp")

    printf 'runs_started=%s\n' "$started"
    printf 'install_success=%s/%s (%s%%)\n' "$finished" "$started" "$rate"
    printf 'first_answer_median=%ss\n' "$median"
    printf 'human_assists=%s\n' "$assisted"
    printf 'log=%s\n' "$LOG"
}

# Feeds the guard the exact shapes it must reject, and a few it must accept.
# This is the evidence that the plan's "everything is redacted" claim is enforced and not
# merely asserted in prose.
cmd_selftest() {
    failures=0
    expect_reject() {
        if ( printf '%s' "$1" | assert_clean >/dev/null 2>&1 ); then
            printf 'selftest: FAIL, guard accepted a secret: %s\n' "$1" >&2
            failures=$((failures + 1))
        else
            printf 'selftest: ok, rejected: %s\n' "$1"
        fi
    }
    expect_accept() {
        if ( printf '%s' "$1" | assert_clean >/dev/null 2>&1 ); then
            printf 'selftest: ok, accepted: %s\n' "$1"
        else
            printf 'selftest: FAIL, guard rejected a safe value: %s\n' "$1" >&2
            failures=$((failures + 1))
        fi
    }

    expect_reject 'Authorization: Bearer eyJhbGciOi'
    expect_reject '-----BEGIN PRIVATE KEY-----'
    expect_reject 'api_key=3f9a2b'
    expect_reject 'token=abc123def'
    expect_reject 'secret: hunter2'
    expect_reject 'sk-abcdefgh12345678'
    expect_reject 'AKIAIOSFODNN7EXAMPLE'
    expect_reject 'ghp_0123456789abcdefghij'

    expect_accept 'T01'
    expect_accept 'macOS-arm64·py3.13'
    # shellcheck disable=SC2088  # a literal tilde path is exactly what a note may contain
    expect_accept '~/.aipotluck/logs/run-20261009.log'
    expect_accept 'no available nodes; the upstream endpoint needs a manual check'
    expect_accept '0.3.0'

    if [ "$failures" -eq 0 ]; then
        printf 'selftest: all cases behaved as required\n'
    else
        printf 'selftest: %s failure(s)\n' "$failures" >&2
        exit 3
    fi
}

cmd_where() {
    printf 'log=%s\n' "$LOG"
    printf 'state=%s\n' "$STATE"
    [ -f "$STATE" ] && printf 'in_progress=%s\n' "$(head -1 "$STATE")"
    return 0
}

# Print the header comment block: everything from line 2 up to the first line
# that is not a comment. Anchoring on the comment shape rather than on a line
# number keeps the help text from silently swallowing implementation lines when
# the header above grows.
usage() {
    awk 'NR > 1 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "$0"
}

case "${1:-}" in
    start)    shift; cmd_start "$@" ;;
    stop)     shift; cmd_stop "$@" ;;
    record)   shift; cmd_record "$@" ;;
    stats)    shift; cmd_stats "$@" ;;
    selftest) shift; cmd_selftest "$@" ;;
    where)    shift; cmd_where "$@" ;;
    ''|-h|--help) usage ;;
    *) die "unknown command $1 (try --help)" ;;
esac

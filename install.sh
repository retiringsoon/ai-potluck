#!/bin/sh
# Install or upgrade the potluck CLI into a self-contained prefix, from source.
#
# Why source and not a release bundle: the first batch of users is small and
# invited, the install has to stay one command, and a pre-built bundle has to
# exist per platform *and* per CPython minor version — a matrix of artifacts
# whose dependency wheels can silently mismatch the machine that unpacks them.
# That mismatch was the F03 defect: a macOS/arm64 cp314 wheel set reached Linux
# and 3.13 users, their offline install failed, and the installer quietly
# continued from the package index while still reporting success. A source
# install has one artifact — the tree at a commit — and one environment built on
# the machine that runs it, so there is nothing to mismatch.
#
# What it checks before changing anything: the machine has to be in the supported
# matrix and a supported CPython has to be reachable, otherwise the install is
# refused with the matrix and the concrete next step printed — before a single
# directory is created. scripts/support_matrix.py holds that table and the two
# READMEs quote it; this script does not restate it.
#
# What it never does: touch $PREFIX/config.json or $PREFIX/keys. Those are the
# user's identity and connection settings; an upgrade that reset them would
# silently mint a second account, which is the one thing the invite flow is
# designed to prevent. Every failure is loud: the failing command's own output
# goes to the terminal and the exit code is non-zero. Nothing here prints
# "installed" after a step that did not succeed.
#
# Usage:
#   ./install.sh                       # install this checkout into ~/.aipotluck
#   ./install.sh --prefix ~/.aipotluck   # default; the CLI's own config dir
#   ./install.sh --python 3.13         # pin the CPython minor version
#   ./install.sh --source "ai-potluck @ git+https://HOST/REPO@TAG"
#   ./install.sh --preflight           # run the compatibility checks and stop
#   ./install.sh --uninstall           # removes venv + launcher, keeps config/keys
#
# Environment: AIPOTLUCK_HOME (prefix), AIPOTLUCK_PYTHON (version).
#
# Exit codes: 0 installed, 1 refused (bad prefix, unsupported platform or
# CPython, no uv, missing source), 2 creating the environment or installing
# failed.

set -eu

die() {
    printf 'install.sh: %s\n' "$1" >&2
    exit "${2:-1}"
}

# Print the header comment block rather than a fixed line range, so the help text
# cannot drift as this header grows.
usage() {
    awk 'NR > 1 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "$0"
}

PREFIX=${AIPOTLUCK_HOME:-"$HOME/.aipotluck"}
PYTHON=${AIPOTLUCK_PYTHON:-}
SOURCE_SPEC=""
UNINSTALL="no"
PREFLIGHT_ONLY="no"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) PREFIX=${2:?--prefix needs a path}; shift 2 ;;
        --python) PYTHON=${2:?--python needs a version}; shift 2 ;;
        --source) SOURCE_SPEC=${2:?--source needs a checkout path or a package spec}; shift 2 ;;
        --preflight) PREFLIGHT_ONLY=yes; shift ;;
        --uninstall) UNINSTALL=yes; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument $1" ;;
    esac
done

# A prefix that is empty or the filesystem root would make the uninstall path
# below destructive, so it is refused before anything else runs.
case "$PREFIX" in
    ""|"/"|"."|"..") die "refusing to use prefix $PREFIX" ;;
esac

VENV="$PREFIX/venv"
BIN="$PREFIX/bin"

if [ "$UNINSTALL" = yes ]; then
    # Only the things this script created are removed; config and keys are the
    # user's data and survive an uninstall on purpose.
    if [ -d "$VENV" ]; then rm -rf "$VENV"; fi
    if [ -f "$BIN/potluck" ]; then rm -f "$BIN/potluck"; fi
    printf 'removed %s and %s/potluck\n' "$VENV" "$BIN"
    printf 'kept %s/config.json and %s/keys (delete them yourself if that is what you want)\n' \
        "$PREFIX" "$PREFIX"
    exit 0
fi

# The matrix lives in one module, which the tests import and the release gate
# reaches through this script. Any Python 3 can run it: it is standard library
# only, and it is *not* the interpreter the install uses — that one is chosen by
# the check itself.
HELPER="$SCRIPT_DIR/scripts/support_matrix.py"
[ -f "$HELPER" ] || die "missing $HELPER
run install.sh from inside an potluck checkout"

BOOT_PY=""
for candidate in python3 python; do
    resolved=$(command -v "$candidate" 2>/dev/null) || continue
    BOOT_PY=$resolved
    break
done
if [ -z "$BOOT_PY" ]; then
    die "need any python3 on PATH to run the compatibility check
install CPython 3.11, 3.12 or 3.13, then rerun ./install.sh"
fi

# Read one SUPPORT_<KEY> value out of a helper run.
support_key() {
    printf '%s\n' "$1" | sed -n "s/^SUPPORT_$2=//p" | head -1
}

SOURCE=${SOURCE_SPEC:-$SCRIPT_DIR}

# The check itself. Its refusal text (the matrix, what to do next) goes straight
# to the user's terminal from the helper, and nothing has been created yet, so a
# refusal leaves the machine exactly as it was.
set -- preflight --source "$SOURCE"
if [ -n "$PYTHON" ]; then set -- "$@" --python "$PYTHON"; fi
PREFLIGHT_OUT=$("$BOOT_PY" "$HELPER" "$@") || {
    printf 'install.sh: refusing to install - nothing was changed\n' >&2
    exit 1
}
PY_VERSION=$(support_key "$PREFLIGHT_OUT" PYTHON_VERSION)
UV_BIN=$(support_key "$PREFLIGHT_OUT" UV)
[ -n "$PY_VERSION" ] || die 'the compatibility check named no CPython version'
[ -n "$UV_BIN" ] || die 'the compatibility check named no uv'
# The helper has already printed its own `preflight ok` line with the host, the
# interpreter and the source; repeating it here would be two lines saying one
# thing. The release gate greps that line to prove the check really ran.

if [ "$PREFLIGHT_ONLY" = yes ]; then
    printf 'install.sh: preflight passed; nothing was installed\n'
    exit 0
fi

# Everything below this line changes the machine.
mkdir -p "$BIN"

# A fresh environment every time, on purpose: the venv holds no user data
# (config.json and keys live outside it), so replacing it is what makes an
# upgrade or a --python change land cleanly instead of layering packages over
# whatever a previous version left behind.
printf 'install.sh: creating %s with CPython %s\n' "$VENV" "$PY_VERSION"
"$UV_BIN" venv --clear --python "$PY_VERSION" "$VENV" \
    || die "could not create the virtualenv in $VENV
the output above is uv's; a version uv cannot resolve needs network access" 2

printf 'install.sh: installing %s\n' "$SOURCE"
"$UV_BIN" pip install --python "$VENV/bin/python" "$SOURCE" \
    || die "installing $SOURCE failed
the output above is uv's; dependencies are fetched from the package index, so
this step needs network access" 2

cat > "$BIN/potluck" <<LAUNCHER
#!/bin/sh
exec "$VENV/bin/potluck" "\$@"
LAUNCHER
chmod 0755 "$BIN/potluck"

printf 'installed potluck into %s\n' "$PREFIX"
"$BIN/potluck" version
case ":$PATH:" in
    *":$BIN:"*) : ;;
    *) printf 'add it to PATH for this shell:\n  export PATH="%s:$PATH"\n' "$BIN" ;;
esac
printf 'next: potluck join --center <url> --invite <code>\n'

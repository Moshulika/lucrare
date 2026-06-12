#!/usr/bin/env bash
set -euo pipefail

REPO="https://github.com/Moshulika/vibe-cli.git"

# Optional-dependency extras to install. Override with VIBE_EXTRAS=... env var,
# or pass `none` to skip extras entirely.
EXTRAS="${VIBE_EXTRAS:-tracing,web,eval}"

# Parse args. `local` installs from the current repo (for development) instead
# of fetching from GitHub.
MODE="remote"
if [[ "${1:-}" == "local" ]]; then
    MODE="local"
fi

# Build the bracketed extras suffix (e.g. "[tracing,web]"), or empty.
if [[ -n "${EXTRAS}" && "${EXTRAS}" != "none" ]]; then
    EXTRAS_SUFFIX="[${EXTRAS}]"
else
    EXTRAS_SUFFIX=""
fi

# Resolve the repo root from the script location so `sh install.sh local` works
# from any cwd, including when piped.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ---------------------------------------------------------
# Colors — matching the vibe-cli purple theme
# ---------------------------------------------------------
P1='\033[38;2;74;14;78m'    # #4A0E4E
P2='\033[38;2;91;18;96m'    # #5B1260
P3='\033[38;2;122;28;125m'  # #7A1C7D
P4='\033[38;2;150;36;156m'  # #96249C
P5='\033[38;2;182;50;189m'  # #B632BD
P6='\033[38;2;211;75;230m'  # #D34BE6
ACC='\033[38;2;167;139;250m' # #a78bfa  (primary accent)
TXT='\033[38;2;224;170;255m' # #E0AAFF  (bright text)
DIM='\033[38;2;109;109;138m' # #6d6d8a
MUT='\033[38;2;82;82;91m'    # #52525b
GRN='\033[38;2;74;222;128m'  # #4ade80
YEL='\033[38;2;251;191;36m'  # #fbbf24
RED='\033[38;2;248;113;113m' # #f87171
BOLD='\033[1m'
RST='\033[0m'

# ---------------------------------------------------------
# Header
# ---------------------------------------------------------
print_header() {
    printf "\n"
    printf "  ${P1}██╗   ██╗██╗██████╗ ███████╗${RST}\n"
    printf "  ${P2}██║   ██║██║██╔══██╗██╔════╝${RST}\n"
    printf "  ${P3}██║   ██║██║██████╔╝█████╗  ${RST}\n"
    printf "  ${P4}╚██╗ ██╔╝██║██╔══██╗██╔══╝  ${RST}\n"
    printf "  ${P5} ╚████╔╝ ██║██████╔╝███████╗${RST}\n"
    printf "  ${P6}  ╚═══╝  ╚═╝╚═════╝ ╚══════╝${RST}\n"
    printf "\n"
    printf "  ${BOLD}${TXT}vibe-cli installer${RST}\n"
    printf "  ${DIM}multi-provider AI chat agent${RST}\n"
    printf "\n"
}

# ---------------------------------------------------------
# Status helpers
# ---------------------------------------------------------
step()  { printf "  ${ACC}●${RST} %b\n" "$*"; }
ok()    { printf "  ${GRN}●${RST} %b\n" "$*"; }
warn()  { printf "  ${YEL}●${RST} %b\n" "$*"; }
fail()  { printf "  ${RED}●${RST} %b\n" "$*" >&2; exit 1; }
dim()   { printf "  ${DIM}%b${RST}\n" "$*"; }

# ---------------------------------------------------------
# Start
# ---------------------------------------------------------
print_header

printf "  ${MUT}─────────────────────────────────${RST}\n"
printf "  ${DIM}Checking dependencies…${RST}\n"
printf "  ${MUT}─────────────────────────────────${RST}\n\n"

# ---------------------------------------------------------
# 1. Check Python
# ---------------------------------------------------------
if command -v python3 >/dev/null 2>&1; then
    PY_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
    ok "Python ${PY_VERSION}"
else
    fail "Python 3.10+ is required. Install it from https://python.org"
fi

# ---------------------------------------------------------
# 2. Ensure uv is available
# ---------------------------------------------------------
if command -v uv >/dev/null 2>&1; then
    UV_VERSION=$(uv --version 2>&1 | awk '{print $2}')
    ok "uv ${UV_VERSION}"
else
    step "Installing uv…"
    curl -LsSf https://astral.sh/uv/install.sh | sh 2>/dev/null
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    command -v uv >/dev/null 2>&1 || fail "uv installation failed. Install manually: https://docs.astral.sh/uv/"
    UV_VERSION=$(uv --version 2>&1 | awk '{print $2}')
    ok "uv ${UV_VERSION} installed"
fi

# ---------------------------------------------------------
# 3. Install vibe-cli
# ---------------------------------------------------------
printf "\n"
printf "  ${MUT}─────────────────────────────────${RST}\n"
printf "  ${DIM}Installing vibe-cli…${RST}\n"
printf "  ${MUT}─────────────────────────────────${RST}\n\n"

if [[ -n "${EXTRAS_SUFFIX}" ]]; then
    dim "Including extras: ${EXTRAS}"
fi

if [[ "${MODE}" == "local" ]]; then
    if [[ ! -f "${REPO_ROOT}/pyproject.toml" ]]; then
        fail "Local install: no pyproject.toml found at ${REPO_ROOT}"
    fi
    step "Installing from local repo (${REPO_ROOT})…"
    if ! uv tool install --force --editable "${REPO_ROOT}${EXTRAS_SUFFIX}"; then
        fail "uv tool install failed. See the error above for details."
    fi
else
    step "Fetching from GitHub…"
    if ! uv tool install --force "vibe-cli${EXTRAS_SUFFIX} @ git+${REPO}"; then
        fail "uv tool install failed. See the error above for details."
    fi
fi

# ---------------------------------------------------------
# 4. Verify & summary
# ---------------------------------------------------------
printf "\n"
printf "  ${MUT}─────────────────────────────────${RST}\n\n"

if command -v vibe >/dev/null 2>&1; then
    ok "vibe-cli installed successfully!"
    printf "\n"
    printf "  ${DIM}Run ${RST}${BOLD}${ACC}vibe${RST}${DIM} to start chatting.${RST}\n"
    printf "\n"
    printf "  ${MUT}────── quick start ──────${RST}\n"
    printf "\n"
    dim "Set at least one API key:"
    printf "  ${ACC}export${RST} OPENAI_API_KEY=${DIM}\"sk-...\"${RST}\n"
    printf "  ${ACC}export${RST} ANTHROPIC_API_KEY=${DIM}\"sk-ant-...\"${RST}\n"
    printf "  ${ACC}export${RST} GOOGLE_API_KEY=${DIM}\"AI...\"${RST}\n"
    printf "\n"
    dim "Or just run vibe — it will prompt you."
    printf "\n"
else
    warn "Installed, but 'vibe' is not on your PATH."
    printf "\n"
    dim "Easiest fix — let uv add its tool dir to your shell profile:"
    printf "\n"
    printf "  ${BOLD}${ACC}uv tool update-shell${RST}\n"
    printf "\n"
    dim "Or add this line to your shell profile manually:"
    printf "\n"
    printf "  ${BOLD}${ACC}export PATH=\"\$HOME/.local/bin:\$PATH\"${RST}\n"
    printf "\n"
    dim "Then restart your shell and run ${BOLD}${ACC}vibe${RST}${DIM}."
    printf "\n"
fi

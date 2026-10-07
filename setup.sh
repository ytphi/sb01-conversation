#!/usr/bin/env bash
# setup.sh — one-command setup for SB01 conversation on Ubuntu/Mac/WSL2
set -e

BOLD="\033[1m"
GREEN="\033[0;32m"
YELLOW="\033[0;33m"
RED="\033[0;31m"
RESET="\033[0m"

info()    { echo -e "${GREEN}[setup]${RESET} $*"; }
warn()    { echo -e "${YELLOW}[warn]${RESET}  $*"; }
error()   { echo -e "${RED}[error]${RESET} $*"; exit 1; }
header()  { echo -e "\n${BOLD}$*${RESET}"; }

OS="$(uname -s)"
ARCH="$(uname -m)"

header "=== SB01 Conversation Setup ==="
info "OS: $OS  ARCH: $ARCH"

# ── Windows guard ────────────────────────────────────────────────────────────
if [[ "$OS" == MINGW* ]] || [[ "$OS" == CYGWIN* ]]; then
    error "Run this script inside WSL2 (Ubuntu), not native Windows.\nSee README for WSL2 install instructions."
fi

# ── 1. System dependencies ───────────────────────────────────────────────────
header "[1/6] System dependencies"

if [ "$OS" = "Linux" ]; then
    info "Installing apt packages..."
    sudo apt-get update -qq
    sudo apt-get install -y \
        cmake build-essential \
        libopenblas-dev liblapack-dev \
        libx11-dev libgtk-3-dev \
        python3-dev python3-venv python3-pip \
        cyclonedds-dev \
        ffmpeg git
    info "apt done."

elif [ "$OS" = "Darwin" ]; then
    if ! command -v brew &>/dev/null; then
        error "Homebrew not found.\nInstall it from https://brew.sh then re-run this script."
    fi
    info "Installing brew packages..."
    brew install cmake ffmpeg git
    info "brew done."
fi

# ── 2. Virtual environment ───────────────────────────────────────────────────
header "[2/6] Virtual environment"

if [ ! -d ".venv" ]; then
    info "Creating .venv..."
    python3 -m venv .venv
else
    info ".venv already exists — skipping creation."
fi
PIP=".venv/bin/pip"
$PIP install --quiet --upgrade pip
# Newer setuptools dropped pkg_resources, which face_recognition_models still needs.
$PIP install --quiet "setuptools<81"
info "venv ready at .venv/ (activate with: source .venv/bin/activate)"

# ── 3. Environment file ──────────────────────────────────────────────────────
header "[3/6] Environment"

# sb01_conversation.py loads "env" (no dot), not ".env" — copy the template
# to the filename that's actually read.
if [ ! -f "env" ]; then
    cp .env.example env
    info "Created env — open it and paste your ANTHROPIC_API_KEY."
else
    info "env already exists — skipping."
fi

# ── 4. Unitree Python SDK ────────────────────────────────────────────────────
header "[4/6] Unitree Python SDK"

if [ ! -d "unitree_sdk2_python" ]; then
    info "Cloning unitree_sdk2_python..."
    git clone https://github.com/unitreerobotics/unitree_sdk2_python.git
else
    info "unitree_sdk2_python/ already exists — skipping clone."
fi

info "Installing CycloneDDS and SDK..."
CDDS_HOME="$(pwd)/.cyclonedds-home"
if [ "$OS" = "Linux" ]; then
    # cyclonedds==0.10.2 ships no wheel for recent CPython, so it must build
    # from source. Building against the OS package (e.g. apt's cyclonedds-dev)
    # is unreliable: on Ubuntu 24.04 its libddsc is installed under a
    # Debian-mangled SONAME (libddsc.so.0debian, not libddsc.so.0), so the
    # resulting extension can't find it again at runtime without extra
    # symlinking. Instead, build the exact 0.10.2 tag from source into a
    # self-contained prefix, matching what the SDK's own README recommends.
    if [ ! -f "$CDDS_HOME/lib/libddsc.so" ]; then
        info "Building CycloneDDS C library (tag 0.10.2) from source..."
        CDDS_SRC="$(mktemp -d)"
        git clone --quiet --depth 1 -b releases/0.10.x https://github.com/eclipse-cyclonedds/cyclonedds "$CDDS_SRC"
        (cd "$CDDS_SRC" && git fetch --quiet --depth 1 origin tag 0.10.2 && git checkout --quiet 0.10.2)
        cmake -S "$CDDS_SRC" -B "$CDDS_SRC/build" -DCMAKE_INSTALL_PREFIX="$CDDS_HOME" -DBUILD_EXAMPLES=OFF -DBUILD_TESTING=OFF >/dev/null
        cmake --build "$CDDS_SRC/build" -j"$(nproc)" >/dev/null
        cmake --build "$CDDS_SRC/build" --target install >/dev/null
        rm -rf "$CDDS_SRC"
    else
        info "CycloneDDS C library already built at .cyclonedds-home/ — skipping."
    fi
    CYCLONEDDS_HOME="$CDDS_HOME" $PIP install --quiet "cyclonedds==0.10.2"
else
    $PIP install --quiet "cyclonedds==0.10.2"
fi
$PIP install --quiet -e unitree_sdk2_python/

# The bundled channel config sets Tracing/Verbosity=config, which triggers a
# buffer overflow in CycloneDDS's own config-printing code
# (ddsi_config.c: do_print_uint32_bitset) under Ubuntu 24.04's stricter
# default hardening (_FORTIFY_SOURCE=3). Reproduces with apt's libddsc and a
# from-source build alike, so it's an upstream bug, not a local build issue —
# strip the Tracing block since it's debug-only logging.
CHANNEL_CONFIG="unitree_sdk2_python/unitree_sdk2py/core/channel_config.py"
if grep -q "<Tracing>" "$CHANNEL_CONFIG" 2>/dev/null; then
    info "Patching channel_config.py to avoid a CycloneDDS config-print crash on 24.04..."
    python3 - "$CHANNEL_CONFIG" <<'PYEOF'
import re, sys
path = sys.argv[1]
text = open(path).read()
text = re.sub(r"\n\s*<Tracing>.*?</Tracing>", "", text, flags=re.DOTALL)
open(path, "w").write(text)
PYEOF
fi

if [ "$OS" = "Linux" ] && ! grep -q "^CYCLONEDDS_HOME=" env 2>/dev/null; then
    echo "CYCLONEDDS_HOME=$CDDS_HOME" >> env
fi
info "SDK installed."

# ── 5. Python packages ───────────────────────────────────────────────────────
header "[5/6] Python packages"

if [ "$OS" = "Darwin" ] && [ "$ARCH" = "arm64" ]; then
    warn "Apple Silicon detected — dlib will compile from source (~5 min). Be patient."
fi
if [ "$OS" = "Linux" ]; then
    warn "dlib will compile from source on Linux (~2-5 min). Be patient."
fi

$PIP install --quiet anthropic edge-tts pydub opencv-python face_recognition
info "Python packages installed."

# ── 6. Memory directories ────────────────────────────────────────────────────
header "[6/6] Memory directories"
mkdir -p memory/faces memory/profiles memory/sessions
info "memory/ subdirectories ready."

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}=== Setup complete! ===${RESET}"
echo ""
echo "Next steps:"
echo ""
echo "  1. Activate the virtual environment (do this in every new shell):"
echo "       source .venv/bin/activate"
echo ""
echo "  2. Add your API key:"
echo "       nano env           # paste your ANTHROPIC_API_KEY"
echo ""
echo "  3. Enroll your face (optional):"
echo "       python3 scripts/enroll_face.py YourName"
echo ""
echo "  4. Connect the G1 via Ethernet, then run:"
echo "       Ubuntu / WSL2:  python3 scripts/sb01_conversation.py eno0"
echo "       Mac:            python3 scripts/sb01_conversation.py en0"
echo ""
echo "  Get a free API key at: https://console.anthropic.com"
echo ""

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
header "[1/5] System dependencies"

if [ "$OS" = "Linux" ]; then
    info "Installing apt packages..."
    sudo apt-get update -qq
    sudo apt-get install -y \
        cmake build-essential \
        libopenblas-dev liblapack-dev \
        libx11-dev libgtk-3-dev \
        python3-dev python3-pip \
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

# ── 2. Unitree Python SDK ────────────────────────────────────────────────────
header "[2/5] Unitree Python SDK"

if [ ! -d "unitree_sdk2_python" ]; then
    info "Cloning unitree_sdk2_python..."
    git clone https://github.com/unitreerobotics/unitree_sdk2_python.git
else
    info "unitree_sdk2_python/ already exists — skipping clone."
fi

info "Installing CycloneDDS and SDK..."
pip3 install --quiet cyclonedds==0.10.2
pip3 install --quiet -e unitree_sdk2_python/
info "SDK installed."

# ── 3. Python packages ───────────────────────────────────────────────────────
header "[3/5] Python packages"

if [ "$OS" = "Darwin" ] && [ "$ARCH" = "arm64" ]; then
    warn "Apple Silicon detected — dlib will compile from source (~5 min). Be patient."
fi

pip3 install --quiet anthropic edge-tts pydub opencv-python face_recognition
info "Python packages installed."

# ── 4. Environment file ──────────────────────────────────────────────────────
header "[4/5] Environment"

if [ ! -f ".env" ]; then
    cp .env.example .env
    info "Created .env — open it and paste your ANTHROPIC_API_KEY."
else
    info ".env already exists — skipping."
fi

# ── 5. Memory directories ────────────────────────────────────────────────────
header "[5/5] Memory directories"
mkdir -p memory/faces memory/profiles memory/sessions
info "memory/ subdirectories ready."

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}=== Setup complete! ===${RESET}"
echo ""
echo "Next steps:"
echo ""
echo "  1. Add your API key:"
echo "       nano .env          # paste your ANTHROPIC_API_KEY"
echo ""
echo "  2. Enroll your face (optional):"
echo "       python3 scripts/enroll_face.py YourName"
echo ""
echo "  3. Connect the G1 via Ethernet, then run:"
echo "       Ubuntu / WSL2:  python3 scripts/sb01_conversation.py eno0"
echo "       Mac:            python3 scripts/sb01_conversation.py en0"
echo ""
echo "  Get a free API key at: https://console.anthropic.com"
echo ""

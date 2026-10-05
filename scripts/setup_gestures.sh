#!/usr/bin/env bash
# setup_gestures.sh — install RoboGesture for scripts/gesture_server.py
# Run on the machine with the NVIDIA GPU (assumed: the robot workstation).
set -euo pipefail

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ROBOGESTURE_DIR="${ROBOGESTURE_DIR:-$HOME/RoboGesture}"
ROBOGESTURE_URL="https://github.com/GalaxyGeneralRobotics/RoboGesture.git"
# gesture_server.py reaches into RoboGesture's internals and was verified
# against exactly this commit. Re-verify the server before changing it.
ROBOGESTURE_COMMIT="2913f1d92751de79a777c4b7b7326266d8c0e32d"
# SHA-256 of checkpoints/motion/model.pt, from RoboGesture's checkpoints/README.md.
MOTION_MODEL_SHA256="313edbd16c166a8dd37b5a4d60e09979be5d502d5b1f9d000d26922019f12427"

info()  { echo "[gestures] $*"; }
error() { echo "[error] $*" >&2; exit 1; }

command -v nvidia-smi >/dev/null || error "nvidia-smi not found — this machine has no working NVIDIA driver.
Run the gesture server on a GPU machine instead and point SB01_GESTURE_URL at it."
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
command -v git-lfs >/dev/null || error "git-lfs missing: sudo apt-get install git-lfs"
command -v "${ROBOGESTURE_PYTHON:-python3.10}" >/dev/null || error "Python 3.10 missing. Newer Ubuntu releases no longer package it; install it with uv:
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH=\"\$HOME/.local/bin:\$PATH\"
  uv python install 3.10
(or set ROBOGESTURE_PYTHON to a 3.10 interpreter)"

if [ ! -e "$ROBOGESTURE_DIR" ]; then
    info "Cloning RoboGesture into $ROBOGESTURE_DIR..."
    git clone "$ROBOGESTURE_URL" "$ROBOGESTURE_DIR"
    git -C "$ROBOGESTURE_DIR" checkout --quiet --detach "$ROBOGESTURE_COMMIT"
else
    info "$ROBOGESTURE_DIR already exists — checking it instead of cloning."
fi

# Whether cloned just now or found: it must be RoboGesture at the verified commit.
[ -d "$ROBOGESTURE_DIR/.git" ] || error "$ROBOGESTURE_DIR is not a git checkout.
Move it aside or set ROBOGESTURE_DIR to another location."
origin="$(git -C "$ROBOGESTURE_DIR" remote get-url origin 2>/dev/null || true)"
case "${origin%.git}" in
    */GalaxyGeneralRobotics/RoboGesture|*:GalaxyGeneralRobotics/RoboGesture) ;;
    *) error "$ROBOGESTURE_DIR is not a RoboGesture checkout (origin: ${origin:-none}).
Move it aside or set ROBOGESTURE_DIR to another location." ;;
esac
head="$(git -C "$ROBOGESTURE_DIR" rev-parse HEAD)"
[ "$head" = "$ROBOGESTURE_COMMIT" ] || error "$ROBOGESTURE_DIR is at commit $head,
but gesture_server.py was verified against $ROBOGESTURE_COMMIT. To switch:
  git -C $ROBOGESTURE_DIR fetch origin && git -C $ROBOGESTURE_DIR checkout --detach $ROBOGESTURE_COMMIT"
info "RoboGesture is at the verified commit ${ROBOGESTURE_COMMIT:0:7}."

cd "$ROBOGESTURE_DIR"
info "Creating RoboGesture's own .venv (PyTorch + CUDA 12.1, several GB)..."
bash scripts/setup.sh

info "Fetching the motion checkpoint and the Mimi audio tokenizer..."
git lfs install --local
git lfs pull

model="checkpoints/motion/model.pt"
[ -f "$model" ] || error "$model is missing after git lfs pull."
if head -c 200 "$model" | grep -q "git-lfs.github.com"; then
    error "$model is still a Git LFS pointer, not the checkpoint. Run: git lfs pull"
fi
echo "$MOTION_MODEL_SHA256  $model" | sha256sum --check --status || error "$model does not match
the published SHA-256 ($MOTION_MODEL_SHA256). Delete it and run: git lfs pull"
info "Motion checkpoint verified (SHA-256 matches)."

.venv/bin/python scripts/download_models.py --only mimi

.venv/bin/python -c "import torch; assert torch.cuda.is_available(), 'CUDA not available to PyTorch'"

echo ""
echo "Gesture setup complete. To use it:"
echo ""
echo "  1. Start the gesture server (leave it running):"
echo "       $ROBOGESTURE_DIR/.venv/bin/python $REPO_DIR/scripts/gesture_server.py"
echo ""
echo "  2. Check the arm channel without moving the robot (robot connected):"
echo "       python3 $REPO_DIR/scripts/check_arm_sdk.py eno0"
echo ""
echo "  3. In another terminal, run the conversation with gestures on:"
echo "       SB01_GESTURE_URL=http://127.0.0.1:8765 python3 scripts/sb01_conversation.py eno0"
echo ""

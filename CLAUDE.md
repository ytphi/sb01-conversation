# Unitree G1 — Project Context for Claude Code

## IMPORTANT: Read these Obsidian notes at the start of every session
```
/home/aloha/Documents/Notes/My_Note/Study/Robotics/SB01 Architecture.md
/home/aloha/Documents/Notes/My_Note/Study/Robotics/unitree-G1/G1 Telecontrol and Data Recording.md
/home/aloha/Documents/Notes/My_Note/Study/Robotics/unitree-G1/SB01 Conversation Loop.md
/home/aloha/Documents/Notes/My_Note/Study/Robotics/unitree-G1/Unitree Hardware.md
/home/aloha/Documents/Notes/My_Note/Study/Robotics/unitree-G1/G1 Rviz.md
```

## Environment
- **Robot:** Unitree G1 humanoid (sb01)
- **Network interface:** `eno0` (Ethernet, 192.168.123.222) → G1 DDS
- **WiFi interface:** `wlp0s20f3` → internet / Claude API
- **Working directory:** `/home/aloha/robotics/platforms/unitree/g1`
- **Python SDK path:** `unitree_sdk2_python/`
- **C++ SDK path:** `../unitree_sdk2/`

## Key Scripts
| Script | Purpose |
|---|---|
| `scripts/sb01_conversation.py` | LLM voice conversation loop (Claude + edge-tts) |
| `scripts/teleop_session.py` | MoCap teleoperation session |
| `scripts/train_gmr.py` | Train GMR retargeting model |
| `scripts/adapter.py` | QTM → G1 joint adapter |
| `scripts/check_robot.py` | Connection and status check |

## DDS Patterns
```python
# Initialize (always first)
ChannelFactoryInitialize(0, "eno0")

# Subscribe with callback
from unitree_sdk2py.core.channel import ChannelSubscriber
sub = ChannelSubscriber("rt/topic_name", MessageType_)
sub.Init(callback_fn, queue_depth=10)

# IDL types: use unitree_hg for G1/H1-2
from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_
from unitree_sdk2py.idl.std_msgs.msg.dds_ import String_
```

## Audio / ASR
- ASR topic: `rt/audio_msg` — JSON string in `String_` type
- `is_final: true` fires after ~1-2s of silence
- `play_state: 1/0` — TTS started/finished
- AudioClient: `TtsMaker(text, speaker_id)`, `LedControl(R,G,B)`, `PlayStream(app, id, pcm)`

## Python SDK Reference
> Source: `unitree_sdk2_python/README.md`

- Python >= 3.8, cyclonedds == 0.10.2
- Replace `enp2s0` in all examples with `eno0` for this machine
- IDL note: `idl/unitree_hg` for G1/H1-2 (not `idl/unitree_go`)

**Run examples:**
```bash
python3 unitree_sdk2_python/example/helloworld/subscriber.py eno0
python3 unitree_sdk2_python/example/g1/audio/g1_audio_client_example.py eno0
python3 unitree_sdk2_python/example/wireless_controller/wireless_controller.py eno0
```

## C++ SDK Reference
> Source: `../unitree_sdk2/README.md`

**Build:**
```bash
cd ../unitree_sdk2 && mkdir -p build && cd build
cmake .. && make
# binaries output to: ../unitree_sdk2/bin/
```

**Run C++ examples:**
```bash
../unitree_sdk2/bin/g1_ankle_swing_example eno0
../unitree_sdk2/bin/g1_arm5_sdk_dds_example eno0
../unitree_sdk2/bin/g1_arm7_sdk_dds_example eno0
```

## G1 Joint Index (arm7 layout)
| Index | Joint |
|---|---|
| 15 | LeftShoulderPitch |
| 16 | LeftShoulderRoll |
| 17 | LeftShoulderYaw |
| 18 | LeftElbow |
| 19-21 | LeftWrist Roll/Pitch/Yaw |
| 22 | RightShoulderPitch |
| 23 | RightShoulderRoll |
| 24 | RightShoulderYaw |
| 25 | RightElbow |
| 26-28 | RightWrist Roll/Pitch/Yaw |

## Dev Log
See `G1_Dev_Log.md` for session history and decisions.

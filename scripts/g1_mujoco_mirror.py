#!/usr/bin/env python3
"""
Live MuJoCo mirror of the real G1 (kinematics only — no physics stepping).

  Solid robot  = measured state   (rt/lowstate: motor q + IMU orientation)
  Ghost robot  = commanded target (rt/lowcmd, or rt/arm_sdk), e.g. GMR teleop output

Every second the terminal prints the joints with the largest |target - measured|,
which is what you want when chasing a retargeting / index-mapping bug.

Run via scripts/run_g1_mirror.sh (sets the Cyclone DDS lib path):
    scripts/run_g1_mirror.sh                       # eno0, ghost from rt/lowcmd
    scripts/run_g1_mirror.sh --cmd-topic rt/arm_sdk
    scripts/run_g1_mirror.sh --ghost-offset 1.0    # ghost side-by-side instead of overlaid
    scripts/run_g1_mirror.sh --no-ghost
    scripts/run_g1_mirror.sh --demo                # no robot: fake motion + fake right-arm error
"""

import argparse
import math
import threading
import time
from pathlib import Path

import mujoco
import mujoco.viewer
import numpy as np

UNITREE_ROOT = Path(__file__).resolve().parents[3]   # unitree/g1/g1-sb01/scripts → unitree
DEFAULT_MODEL = UNITREE_ROOT / "TEXEDO/assets/robot/g1/g1_29dof_rev_1_0.xml"

# Motor index 0..28 on G1 29-DoF (unitree_hg). Mapped to the MuJoCo model by name,
# so any G1 XML works regardless of its joint order.
G1_JOINTS = [
    "left_hip_pitch_joint", "left_hip_roll_joint", "left_hip_yaw_joint",
    "left_knee_joint", "left_ankle_pitch_joint", "left_ankle_roll_joint",
    "right_hip_pitch_joint", "right_hip_roll_joint", "right_hip_yaw_joint",
    "right_knee_joint", "right_ankle_pitch_joint", "right_ankle_roll_joint",
    "waist_yaw_joint", "waist_roll_joint", "waist_pitch_joint",
    "left_shoulder_pitch_joint", "left_shoulder_roll_joint", "left_shoulder_yaw_joint",
    "left_elbow_joint", "left_wrist_roll_joint", "left_wrist_pitch_joint", "left_wrist_yaw_joint",
    "right_shoulder_pitch_joint", "right_shoulder_roll_joint", "right_shoulder_yaw_joint",
    "right_elbow_joint", "right_wrist_roll_joint", "right_wrist_pitch_joint", "right_wrist_yaw_joint",
]
N = len(G1_JOINTS)
ARM_SDK_JOINTS = range(12, 29)  # rt/arm_sdk only commands waist + arms
PELVIS_HEIGHT = 0.793           # m; IMU gives orientation only
GHOST_RGBA = (0.2, 0.8, 1.0, 0.35)
STALE_S = 0.5


class RobotState:
    """Latest measured + commanded state, written by DDS threads, read by the viewer."""

    def __init__(self):
        self.lock = threading.Lock()
        self.q_meas = np.zeros(N)
        self.quat = np.array([1.0, 0.0, 0.0, 0.0])  # w, x, y, z
        self.q_cmd = None
        self.t_meas = None
        self.t_cmd = None

    def on_lowstate(self, msg):
        q = np.array([msg.motor_state[i].q for i in range(N)])
        quat = np.array(msg.imu_state.quaternion, dtype=float)
        with self.lock:
            self.q_meas, self.quat, self.t_meas = q, quat, time.time()

    def on_cmd(self, msg, joints=range(N)):
        with self.lock:
            q = self.q_meas.copy() if self.q_cmd is None else self.q_cmd.copy()
            for i in joints:
                q[i] = msg.motor_cmd[i].q
            self.q_cmd, self.t_cmd = q, time.time()

    def snapshot(self):
        with self.lock:
            now = time.time()
            cmd_fresh = self.t_cmd is not None and now - self.t_cmd < STALE_S
            return (self.q_meas.copy(), self.quat.copy(),
                    self.q_cmd.copy() if cmd_fresh else None, self.t_meas)


def demo_update(state: RobotState, t: float):
    """Fake motion: measured lags the command, right arm has a constant offset."""
    def pose(tt):
        q = np.zeros(N)
        s = math.sin(tt)
        q[12] = 0.3 * math.sin(0.5 * tt)
        q[15], q[16], q[18] = -0.8 + 0.6 * s, 0.3, 0.8 + 0.4 * s
        q[22], q[23], q[25] = -0.8 - 0.6 * s, -0.3, 0.8 - 0.4 * s
        return q
    cmd = pose(t)
    meas = pose(t - 0.15)
    meas[22] += 0.25  # simulated right shoulder pitch error
    yaw = 0.1 * math.sin(0.3 * t)
    with state.lock:
        state.q_cmd, state.t_cmd = cmd, time.time()
        state.q_meas, state.t_meas = meas, time.time()
        state.quat = np.array([math.cos(yaw / 2), 0.0, 0.0, math.sin(yaw / 2)])


def joint_qpos_addrs(model):
    addrs = []
    for name in G1_JOINTS:
        jid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, name)
        if jid < 0:
            raise SystemExit(f"Joint '{name}' not found in model — is this a 29-DoF G1 XML?")
        addrs.append(model.jnt_qposadr[jid])
    return np.array(addrs)


def free_joint_addr(model):
    for j in range(model.njnt):
        if model.jnt_type[j] == mujoco.mjtJoint.mjJNT_FREE:
            return model.jnt_qposadr[j]
    return None


def set_pose(model, data, qadr, fadr, q, quat, offset_y=0.0):
    data.qpos[qadr] = q
    if fadr is not None:
        data.qpos[fadr:fadr + 3] = (0.0, offset_y, PELVIS_HEIGHT)
        data.qpos[fadr + 3:fadr + 7] = quat / (np.linalg.norm(quat) or 1.0)
    mujoco.mj_forward(model, data)


def draw_ghost(viewer, model, ghost_data):
    scn = viewer.user_scn
    scn.ngeom = 0
    mujoco.mjv_addGeoms(model, ghost_data, viewer.opt, viewer.perturb,
                        mujoco.mjtCatBit.mjCAT_DYNAMIC, scn)
    for i in range(scn.ngeom):
        scn.geoms[i].rgba[:] = GHOST_RGBA
        scn.geoms[i].segid = -1  # not selectable


def report_error(q_meas, q_cmd, top=3):
    err = q_cmd - q_meas
    idx = np.argsort(-np.abs(err))[:top]
    parts = [f"[{i:2d}] {G1_JOINTS[i].removesuffix('_joint'):<22} {math.degrees(err[i]):+6.1f}°"
             for i in idx]
    print(f"max |target-measured|: " + "   ".join(parts), flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("iface", nargs="?", default="eno0")
    ap.add_argument("--model", type=Path, default=DEFAULT_MODEL, help="G1 29-DoF MuJoCo XML")
    ap.add_argument("--cmd-topic", default="rt/lowcmd", choices=["rt/lowcmd", "rt/arm_sdk"])
    ap.add_argument("--no-ghost", action="store_true", help="measured state only")
    ap.add_argument("--ghost-offset", type=float, default=0.0, help="shift ghost along y (m)")
    ap.add_argument("--no-imu", action="store_true", help="keep pelvis upright, ignore IMU")
    ap.add_argument("--demo", action="store_true", help="fake data, no robot needed")
    ap.add_argument("--fps", type=float, default=50.0)
    args = ap.parse_args()

    model = mujoco.MjModel.from_xml_path(str(args.model))
    data = mujoco.MjData(model)
    ghost = mujoco.MjData(model)
    qadr, fadr = joint_qpos_addrs(model), free_joint_addr(model)

    state = RobotState()
    if not args.demo:
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
        from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowCmd_, LowState_
        ChannelFactoryInitialize(0, args.iface)
        subs = [ChannelSubscriber("rt/lowstate", LowState_)]
        subs[0].Init(state.on_lowstate, 10)
        if not args.no_ghost:
            joints = ARM_SDK_JOINTS if args.cmd_topic == "rt/arm_sdk" else range(N)
            subs.append(ChannelSubscriber(args.cmd_topic, LowCmd_))
            subs[1].Init(lambda m: state.on_cmd(m, joints), 10)
        print(f"Listening on {args.iface}: rt/lowstate"
              + ("" if args.no_ghost else f" + {args.cmd_topic} (ghost)"), flush=True)
    else:
        print("Demo mode: fake motion, right shoulder pitch has a +14° simulated error", flush=True)

    t0 = time.time()
    last_report = last_warn = 0.0
    with mujoco.viewer.launch_passive(model, data) as viewer:
        viewer.cam.lookat[:] = (0.0, args.ghost_offset / 2, 0.8)
        viewer.cam.distance, viewer.cam.azimuth, viewer.cam.elevation = 3.0, 150, -15
        while viewer.is_running():
            tick = time.time()
            if args.demo:
                demo_update(state, tick - t0)
            q_meas, quat, q_cmd, t_meas = state.snapshot()
            if args.no_imu:
                quat = np.array([1.0, 0.0, 0.0, 0.0])

            if t_meas is None and tick - last_warn > 2.0:
                print("No rt/lowstate yet — is the G1 on and eno0 up?", flush=True)
                last_warn = tick

            with viewer.lock():
                set_pose(model, data, qadr, fadr, q_meas, quat)
                if q_cmd is not None and not args.no_ghost:
                    set_pose(model, ghost, qadr, fadr, q_cmd, quat, args.ghost_offset)
                    draw_ghost(viewer, model, ghost)
                else:
                    viewer.user_scn.ngeom = 0
            viewer.sync()

            if q_cmd is not None and tick - last_report > 1.0:
                report_error(q_meas, q_cmd)
                last_report = tick

            time.sleep(max(0.0, 1.0 / args.fps - (time.time() - tick)))


if __name__ == "__main__":
    main()

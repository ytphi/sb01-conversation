#!/usr/bin/env python3
"""
preview_talk_gestures.py  -  check Yotie's talking gestures in MuJoCo before the robot

Plays sb01/talk_gestures.TalkMotion through a scripted reply covering every style
(fist-bump greeting, self, open, explain, you, list, wide, shrug) from the G1 walk-mode
rest pose, then:
  - prints peak joint speed, largest offset, joint-limit margin, arm-body contacts
  - renders a labelled filmstrip PNG (front + side view), one frame per style at its peak

Usage (needs mujoco, e.g. the texedo310 env):
  MUJOCO_GL=egl python3 scripts/preview_talk_gestures.py [--rest rest_q.npy] [--out preview.png]
"""
import argparse, os, sys
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from sb01.talk_gestures import TalkMotion, ARM_JOINTS, CONTROL_DT, MAX_VEL, pick_style
from teleop.retarget import G1_JOINT_LIMITS, G1_JOINT_NAMES

MODEL = os.path.expanduser("~/robotics/platforms/unitree/TEXEDO/assets/robot/g1/g1_29dof_rev_1_0.xml")
# Arm pose measured on the real G1 in walk mode (rt/lowstate, 2026-10-07)
REST_ARMS = [0.204, 0.207, -0.003, 1.178, -0.117, -0.002, 0.117,
             0.206, -0.159, -0.020, 1.190, 0.106, -0.039, -0.172]
# (start s, text or None, style cue) — one reply, sentences spaced like real speech
SCRIPT = [
    (0.0,  "Hey Yutong! Good to see you.", "fistbump"),
    (5.5,  "I'm Yotie, a Unitree G1 humanoid robot.", None),
    (9.5,  "Welcome to the lab!", None),
    (12.5, "This lab works on embodied AI and motion capture.", None),
    (16.0, "You can ask me anything about the course.", None),
    (19.5, "First, we record the motion, then we retarget it.", None),
    (23.0, "Robots like me could help everyone.", None),
    (26.5, "I'm not sure about that one.", None),
]
END = 30.0


def simulate(rest, seed=1):
    m = TalkMotion(seed=seed)
    m.begin(0.0)
    qs, ts, labels = [], [], []
    pending = list(SCRIPT)
    t = 0.0
    while t < END + 2.0:
        while pending and t >= pending[0][0]:
            _, text, cue = pending.pop(0)
            m.sentence(t, text, cue)
            labels.append((t, cue or pick_style(text), m.last_style))
        if t >= END and m.speaking:
            m.end(t)
        q = rest.copy(); q[ARM_JOINTS] += m.offsets(t)
        qs.append(q); ts.append(t)
        t += CONTROL_DT
    return np.array(ts), np.array(qs), labels


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rest", help="29-joint rest pose .npy (default: measured walk-mode arms)")
    ap.add_argument("--out", default="talk_gestures_preview.png")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()
    rest = np.load(args.rest) if args.rest else np.zeros(29)
    if not args.rest:
        rest[ARM_JOINTS] = REST_ARMS

    ts, qs, labels = simulate(rest, args.seed)
    off = qs[:, ARM_JOINTS] - rest[ARM_JOINTS]
    vel = np.abs(np.diff(qs[:, ARM_JOINTS], axis=0)) / CONTROL_DT
    lim = np.array([G1_JOINT_LIMITS[G1_JOINT_NAMES[j]] for j in ARM_JOINTS])
    margin = np.minimum(qs[:, ARM_JOINTS] - lim[:, 0], lim[:, 1] - qs[:, ARM_JOINTS]).min()
    names = [G1_JOINT_NAMES[j].replace("_joint", "") for j in ARM_JOINTS]
    print("styles played:", ", ".join(f"{t:.1f}s {played}" for t, _, played in labels))
    print(f"largest offset : {np.abs(off).max():.3f} rad ({names[np.abs(off).max(0).argmax()]})")
    print(f"peak speed     : {vel.max():.3f} rad/s ({names[vel.max(0).argmax()]})  [safety cap {MAX_VEL}]")
    print(f"limit margin   : {margin:.3f} rad (closest approach to any joint limit)")
    print(f"back at rest   : {np.abs(off[-1]).max():.4f} rad at t={ts[-1]:.1f}s")

    import mujoco
    model = mujoco.MjModel.from_xml_path(MODEL); data = mujoco.MjData(model)
    model.geom_margin[:] = 0.03          # count anything closer than 3 cm as a contact
    hinge = [model.joint(i).name for i in range(model.njnt) if model.joint(i).type == 3]

    def set_q(q):
        for k, n in enumerate(hinge):
            data.qpos[model.joint(n).qposadr[0]] = q[k]
        data.qpos[2] = 0.79; data.qpos[3:7] = [1, 0, 0, 0]
        mujoco.mj_forward(model, data)

    arm = lambda b: any(k in b for k in ("shoulder", "elbow", "wrist", "hand", "rubber"))
    worst = 0
    for q in qs[::5]:
        set_q(q)
        n = 0
        for i in range(data.ncon):
            c = data.contact[i]
            b1 = model.body(model.geom_bodyid[c.geom1]).name; b2 = model.body(model.geom_bodyid[c.geom2]).name
            if arm(b1) != arm(b2) or ("left" in b1 and "right" in b2) or ("right" in b1 and "left" in b2):
                n += 1
        worst = max(worst, n)
    print(f"arm contacts   : {worst} (closer than 3 cm, max over the whole reply; arm vs body or arm vs arm)")

    # one frame per style, at the middle of its hold (or peak)
    frames = [(0.0, "rest")] + [(t + 1.6, played) for t, _, played in labels]
    r = mujoco.Renderer(model, height=260, width=300)
    rows = []
    for az in (180, 90):
        row = []
        for ft, lab in frames:
            set_q(qs[np.argmin(np.abs(ts - ft))])
            cam = mujoco.MjvCamera(); cam.lookat[:] = [0, 0, 1.0]; cam.distance = 1.6
            cam.azimuth, cam.elevation = az, -8
            r.update_scene(data, cam); row.append(r.render())
        rows.append(np.concatenate(row, axis=1))
    img = np.concatenate(rows, axis=0)
    from PIL import Image, ImageDraw
    im = Image.fromarray(img); dr = ImageDraw.Draw(im)
    for i, (_, lab) in enumerate(frames):
        dr.text((i * 300 + 8, 8), lab, fill=(255, 255, 0))
    im.save(args.out)
    print(f"filmstrip      : {args.out}  ({' | '.join(l for _, l in frames)})")


if __name__ == "__main__":
    main()

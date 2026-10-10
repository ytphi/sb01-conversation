#!/usr/bin/env python3
"""
Bridge G1 rt/lowstate (Unitree DDS) -> ROS 2 /joint_states + world->pelvis TF,
so RViz2 can show the real robot live.

Run via scripts/run_g1_rviz.sh (sources ROS 2 Jazzy + sets the Cyclone DDS lib path).

    python3 g1_rviz_bridge.py eno0          # live robot
    python3 g1_rviz_bridge.py --demo        # no robot: waves the arms (pipeline test)
"""

import argparse
import math
import threading
import time

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from sensor_msgs.msg import JointState
from geometry_msgs.msg import TransformStamped
from tf2_ros import TransformBroadcaster

# Motor index 0..28 on G1 29-DoF == joint order in rviz/g1_29dof.urdf
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
PELVIS_HEIGHT = 0.793  # m, standing; IMU gives orientation only, not position


class G1RvizBridge(Node):
    def __init__(self, demo: bool, rate_hz: float):
        super().__init__("g1_rviz_bridge")
        self.js_pub = self.create_publisher(JointState, "joint_states", 10)
        self.tf_pub = TransformBroadcaster(self)
        self.demo = demo
        self.lock = threading.Lock()
        self.q = [0.0] * len(G1_JOINTS)
        self.dq = [0.0] * len(G1_JOINTS)
        self.tau = [0.0] * len(G1_JOINTS)
        self.quat = (1.0, 0.0, 0.0, 0.0)  # w, x, y, z
        self.last_msg = None
        self.t0 = time.time()
        self.create_timer(1.0 / rate_hz, self.publish)
        self.create_timer(2.0, self.watchdog)

    # Called from the Unitree DDS thread
    def on_lowstate(self, msg):
        with self.lock:
            for i in range(len(G1_JOINTS)):
                m = msg.motor_state[i]
                self.q[i], self.dq[i], self.tau[i] = m.q, m.dq, m.tau_est
            self.quat = tuple(msg.imu_state.quaternion)
            self.last_msg = time.time()

    def demo_step(self):
        t = time.time() - self.t0
        s = math.sin(t)
        with self.lock:
            self.q = [0.0] * len(G1_JOINTS)
            self.q[15] = -0.8 + 0.6 * s          # left shoulder pitch
            self.q[16] = 0.3                      # left shoulder roll
            self.q[18] = 0.8 + 0.4 * s            # left elbow
            self.q[22] = -0.8 - 0.6 * s           # right shoulder pitch
            self.q[23] = -0.3                     # right shoulder roll
            self.q[25] = 0.8 - 0.4 * s            # right elbow
            self.q[12] = 0.3 * math.sin(0.5 * t)  # waist yaw
            self.last_msg = time.time()

    def publish(self):
        if self.demo:
            self.demo_step()
        with self.lock:
            q, dq, tau, quat = list(self.q), list(self.dq), list(self.tau), self.quat
        now = self.get_clock().now().to_msg()

        js = JointState()
        js.header.stamp = now
        js.name = G1_JOINTS
        js.position, js.velocity, js.effort = q, dq, tau
        self.js_pub.publish(js)

        tf = TransformStamped()
        tf.header.stamp = now
        tf.header.frame_id = "world"
        tf.child_frame_id = "pelvis"
        tf.transform.translation.z = PELVIS_HEIGHT
        w, x, y, z = quat
        tf.transform.rotation.w, tf.transform.rotation.x = float(w), float(x)
        tf.transform.rotation.y, tf.transform.rotation.z = float(y), float(z)
        self.tf_pub.sendTransform(tf)

    def watchdog(self):
        if self.last_msg is None:
            self.get_logger().warn("No rt/lowstate yet — is the G1 on and eno0 up?")
        elif time.time() - self.last_msg > 1.0:
            self.get_logger().warn(f"rt/lowstate stale for {time.time() - self.last_msg:.1f}s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("iface", nargs="?", default="eno0")
    ap.add_argument("--demo", action="store_true", help="fake motion, no robot needed")
    ap.add_argument("--rate", type=float, default=50.0, help="publish rate (Hz)")
    args = ap.parse_args()

    rclpy.init()
    node = G1RvizBridge(args.demo, args.rate)

    if not args.demo:
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
        from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_
        ChannelFactoryInitialize(0, args.iface)
        sub = ChannelSubscriber("rt/lowstate", LowState_)
        sub.Init(node.on_lowstate, 10)
        node.get_logger().info(f"Subscribed to rt/lowstate on {args.iface}")
    else:
        node.get_logger().info("Demo mode: publishing fake arm motion")

    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()

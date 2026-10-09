# Week 2 (Aug 31 and Sep 2): How do robots perceive the physical world?

Readings: Probabilistic Robotics, chapter 1; "Open X-Embodiment: Robotic Learning Datasets and RT-X Models".

## Key idea
Robots do not see the world; they measure it. The central problem of robotics is making decisions under uncertainty. Instead of "the robot is here" we say "the robot is probably here, with 85% confidence."
- Sensors are noisy, motors are imperfect, and the world keeps changing.
- A robot maintains a belief about the world. Every sensor reading updates that belief and every movement changes it. Better probability estimates lead to safer decisions.

## Paper: Open X-Embodiment
- Question: can robots learn together the way large language models do? Today robotics is like training GPT from scratch for every company.
- Traditional robot learning: collect data on robot A, train, and the model works only on robot A; robot B starts from zero.
- Their approach: collect demonstrations from 22 robot embodiments at 21 institutions (over 1 million trajectories, 527 skills), convert them into one standardized dataset, and train one transformer, RT-X, so knowledge transfers between robots.
- Findings: 75.8% success on emergent skills against 27.3% with a single-robot dataset, and strong generalization to unseen objects, new environments and natural-language instructions.
- Discussion question: if every university contributed demonstrations, could we build a "GPT for robots", and what would still stand in the way?

## Sensors and what they measure
- Camera: images (object detection, face recognition). Depth camera: distance (3D perception, grasping). The G1 and Go2 use the Intel RealSense D435i.
- LiDAR: 3D geometry (SLAM, navigation). IMU: acceleration and rotation (balance, localization). Joint encoder: joint angle (motion control).
- Force sensor: contact force. Tactile sensor: touch. Microphone: sound for speech recognition. GPS or UWB: position. Speaker: audio output.
- Robots combine several sensors (sensor fusion) because each one alone is incomplete or uncertain.

Details from class:
- Vision: CMOS or CCD sensors capture light and the lens projects the 3D scene onto a 2D image, which introduces distortion. Camera calibration with OpenCV finds the camera's intrinsic parameters and corrects distortion. Frames are turned into feature embeddings, and cosine similarity compares them for recognition.
- IMU: accelerometers measure linear acceleration and gyroscopes measure angular velocity on three axes. Dead reckoning integrates these to track position and orientation, but small biases accumulate into drift over time.
- LiDAR: an active sensor that sends its own laser pulses, so it works in low light. It measures distance by time of flight and produces dense 3D point clouds.
- Microphones: an array can locate a sound source from arrival times. Onboard signal processing handles noise reduction, echo suppression and beamforming.

## Math for position and orientation
- Rotation matrix: rotates points or frames. Example: rotating the point (1, 0, 0) by 90 degrees about the Z axis gives (0, 1, 0).
- Euler angles: orientation as three sequential rotations, roll about X, pitch about Y, yaw about Z. Each rotation changes the frame for the next one.
- Axis-angle: one rotation axis and one angle. More compact than Euler angles and free from gimbal lock. Converted to a rotation matrix with Rodrigues' formula (no need to memorize it).
- Quaternion: four numbers (w, x, y, z) that describe a 3D rotation without gimbal lock.
- Transformation matrix: combines a rotation R and a translation t into one matrix giving a complete pose in 3D space.
- Several coordinate systems are needed (world, robot, camera, sensor), and transformations move information between them.

## Kinematics
- DH (Denavit-Hartenberg) parameters: four parameters per joint that describe a serial robot's geometry. Multiplying the joints' transformation matrices gives the pose of any link.
- Forward kinematics (FK): from known joint angles, compute the position and orientation of the end effector.
- Inverse kinematics (IK): from a desired position and orientation, compute the joint angles needed.

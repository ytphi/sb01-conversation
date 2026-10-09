# Week 1 (Aug 24 and 26): What is embodied AI?

## Embodied AI
- Why it is interesting: an AI model has to solve dynamic problems in the physical world, and studying it helps us understand people.
- Potential uses raised in class: home chores, a personal tutor and companion.
- How to stay grounded: learn more math, and look at what is happening in a modular way.
- World models (Yann LeCun): JEPA learns how the world works by predicting abstract future representations rather than reconstructing future pixels.

## Our robots
- Unitree G1 (G1-U2, about $39,400): humanoid, 127 cm (4 ft 2 in), about 35 kg, 29 degrees of freedom, walks up to 2 m/s; walking, balancing, stair climbing and dynamic motion. Battery: 9000 mAh quick-swap lithium, about 2 hours depending on use. Sensors: IMU, joint encoders, microphone array, speakers, a MID360 LiDAR and a D435i depth camera. Focus: embodied AI, human motion, dexterous skills.
- Unitree Go2 Edu (about $13,900): quadruped, about 15 kg, 70 x 31 x 40 cm, payload about 8 kg (limit about 12 kg), up to 3.7 m/s, 2-4 hour battery, climbs slopes up to 40 degrees, 12 degrees of freedom (3 per leg), NVIDIA Jetson Orin NX computer, Wi-Fi 6 and Bluetooth. Focus: robotics, navigation, autonomous mobility.
- Unitree R1 (about $14,750): humanoid, 123 cm, about 29 kg, 26-40 degrees of freedom in the EDU configuration (6 per leg, 5 per arm, 2 waist, 2 head), runs up to about 2.5 m/s, 1-2 hour battery, stereo RGB cameras, onboard multimodal model for voice and vision. Focus: human-robot interaction and everyday applications.
- Mobile ALOHA: imitation learning; the model predicts the next 45 robot actions; parts are 3D printable.

## Lab tracks
- Track E, simulation with Lucky Engine: Windows or Mac station or a personal machine. The default for everyone in week 1, with more structured labs.
- Track L, Linux full-stack: SSH, the real G1 hardware and the full SDK. More freedom and less scaffolding; students can opt in from week 2.
- Lucky Engine and the physical G1 are both fully valid demo platforms. Lucky Engine is not a fallback.
- Final projects are open "quick prototypes": any combination of what the modules covered.
- G1 orientation practice: power button, battery, main joints, emergency stop, safe startup and shutdown, fall recovery, proper lifting, movement control, and safety zones around the robot.

## Why robots need mathematics
Robots do not understand the world; they understand numbers. Mathematics turns those numbers into perception, motion and intelligence.
- Camera: RGB pixel values 0-255. LiDAR: x, y, z point coordinates. IMU: acceleration in m/s^2 and angular velocity in rad/s. Microphone: audio waveform amplitudes. Joint encoder: angles in radians or degrees.
- Cartesian coordinates answer "Where am I?": positions in 2D and 3D, and the basis of kinematics and navigation. The same object has different coordinates in the world frame, robot frame, camera frame and end effector frame. Everything starts with position.
- Vectors answer "How do I move?": position, velocity, force, camera direction and joint states are all vectors.
- Matrices answer "How do I transform information?": they organize and transform data, represent spatial relationships, and let AI models do billions of calculations in parallel.
- Probability answers "How certain am I?": sensors are uncertain, for example a distance of 2.35 m plus or minus 0.03 m, or an object label with 92% confidence. Combine uncertain information from several sensors before acting.
- Optimization answers "What should I do next?": finding the best solution while satisfying constraints.
- Summary line from class: robotics is applied mathematics that enables intelligent machines to perceive, move and make decisions.

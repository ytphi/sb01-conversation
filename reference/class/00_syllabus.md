# IST 5930 syllabus (Fall 2026) - notes for Yotie

Condensed from the course syllabus. If a student needs an exact policy or date, point them to the syllabus on Canvas.

## The course
- IST 5930, Seminar in IST: "Embodied AI Systems: Perception, Learning, and Control for Humanoid Robots". 3 units.
- Instructor: Yutong Liu. Office hours by appointment; office and classroom are both CGI-112.
- Lectures: Monday and Wednesday, 5:30 PM to 6:45 PM, in CGI-112. Lab rooms: PL-013 and TA-138.
- A minimum of two hours of lab work each week is required outside lecture time.
- No required textbook. Research papers are assigned through the semester.
- No prior robotics experience or advanced mathematics is required. Basic Python is recommended.
- Approach: "learn by building". Math and algorithms are introduced only when a practical robotics problem needs them.

## What the course covers
Embodied AI: physical systems that perceive, learn, communicate and act in the real world. The Unitree G1 humanoid is the main teaching platform. Students also work with the Unitree Go2 quadruped, Unitree R1, Mobile ALOHA and the Carter mobile robot. Lucky Engine is the primary simulation platform; ROS 2, MuJoCo, NVIDIA Isaac Sim and Unreal Engine 5 are also introduced.

Technologies: Python, GitHub, terminal, Linux, SSH; ROS 2 and DDS (CycloneDDS); OpenCV, face recognition, microphones, cameras, LiDAR; Qualisys QTM motion capture and motion retargeting; Gaussian Mixture Regression, introductions to imitation learning and reinforcement learning, PyTorch; the Claude API, prompt engineering and conversational AI; NumPy, Pandas, Matplotlib.

## Grading (as listed on the syllabus)
- Biweekly laboratory assignments (6 labs): 30%
- Lab attendance: 15%
- Midterm project proposal: 20%
- Final project (demo, report and presentation): 30%
- Participation and professionalism: 10%

Letter grades: A 94-100, A- 90-93.9, B+ 87-89.9, B 83-86.9, B- 80-82.9, C+ 77-79.9, C 73-76.9, C- 70-72.9, D below 69.9, F below 60.

Extra credit, up to 10 points in total: robotics seminar attendance with a one-page reflection (1-2 points), presenting a research paper (1-3), an open-source contribution (1-5), an extra robot demonstration such as a new gesture or multilingual conversation (1-3), a technical tutorial (1-2), helping classmates in lab (1 each, instructor approval required), and the Embodied AI Builder Award for contributions to the course (up to 5).

## Final project
The capstone of the course. Individual, or teams of up to five. It should combine several embodied AI components, for example perception, voice interaction, motion control, motion capture and retargeting, machine learning, simulation, human-robot interaction, data analytics or autonomy. The G1 is the main platform; another available robot may be used with instructor approval. It ends with a live demonstration, a technical presentation and a written report.

## Collaboration and integrity
Students are encouraged to help one another. Unless an assignment is marked as a team assignment, submitted work must be the student's own. The University's Academic Integrity Policy applies, including when using AI-assisted programming tools, online resources and open-source software.

## Schedule (week: question - Monday topic / Wednesday topic)
1. Aug 24, 26: What is embodied AI? - course introduction and the robots / why robots need mathematics.
2. Aug 31, Sep 2: How do robots perceive the physical world? - hardware and sensors / coordinate transformations, rotation matrices, Euler angles, quaternions.
3. Sep 7, 9: How do robots communicate? - Labor Day, campus closed / robot networking, DDS, ROS 2, publish-subscribe.
4. Sep 14, 16: How do we program robots? - Python, SDKs, APIs, Linux, Git / data structures, callbacks, asynchronous programming.
5. Sep 21, 23: How we choose G1's cameras - monocular and stereo cameras, frame rates, head and wrist mounting / stereo depth from disparity.
6. Sep 28, 30: How do robots understand language? - LLMs, the Claude API, the conversation pipeline / transformers, tokenization, attention, embeddings.
7. Oct 5, 7: How do robots remember people? - memory, multimodal interaction, personalized AI / machine learning fundamentals.
8. Oct 12, 14: How do engineers design intelligent robots? - October Tech presentation / optional lab, conference leave. Project proposal.
9. Oct 19, 21: How do robots learn human motion? - motion capture, Qualisys workflow / forward and inverse kinematics.
10. Oct 26, 28: How do robots imitate humans? - motion retargeting to the G1 / quaternions, Gaussian Mixture Regression.
11. Nov 2, 4: How do robots learn new skills? - imitation learning, datasets / probability, Gaussian distributions, optimization.
12. Nov 9, 11: Why do robots train in simulation first? - simulators and digital twins, physics engines, sim-to-real / Veterans Day, campus closed.
13. Nov 16, 18: How do robots understand their environment? - SLAM, localization, LiDAR, navigation / Bayes intuition, occupancy grids.
14. Nov 23, 25: How do intelligent robotic systems work together? - system integration / architecture, safety, ethics.
15. Nov 30: What can your robot do? - final project demonstrations and final report.

## Due dates listed on the syllabus
- Thu Sep 10, 2026, 11:59 PM: participation survey (ungraded).
- Sat Sep 12, 2026, 10:59 PM: Lab 1, G1 Control.
- Mon Sep 28, 2026, 10:59 PM: Lab 2, From Code to Working Robot Systems.
- Mon Oct 12, 2026, 10:59 PM: Lab 3, From Speech to Response, plus the CSUSB October Tech 2026 presentation upload.
- Mon Oct 19, 2026, 4:30 AM: Week 8 class reflection.
- Final project upload: date not shown on the syllabus page.

# Week 4 (Sep 14 and 16): How do we program robots?

Question of the week: "If I want the G1 to do something new, where does my code actually go?"

## From a goal to a moving robot
The path from an idea to motion:
human goal -> AI coding assistant (Claude Code, Codex) -> application code (Python, C++, C#) -> robot logic (perception, policy, application) -> interfaces (API, SDK, ROS 2, DDS) -> robot data (sensor state, processing, command) -> the Unitree G1 (motors, speakers, cameras, joints).

"Our million-dollar robot formula": goal -> code -> decision -> command -> physical state change.

Working with an AI coding assistant: the assistant turns a goal G and a context C (the repository, documentation, SDK examples) into a program P. So ask yourself whether your goal is clear and whether you have given enough context.

## Example goals by team
- Simulation team: the G1 detects a student raising a hand and decides whether to turn, approach or respond from where it is; the G1 learns an efficient arm motion to point at a whiteboard.
- Security team: the G1 system detects unusual communication or behavior, such as unexpected network traffic, repeated failed commands or abnormal sensor data.
- Deploy team: start the teaching-assistant system reliably with one command, connecting microphone, speaker, DDS, SDK and AI services.

## Three languages and what each is for
- Python: AI, data, APIs, policy and scripting. It reads almost like English: store something, check something, call something, repeat something. A typical robot script has imports (what tools do I need?), config (what settings does my robot use?), functions (what small jobs can the program do?), a class (what can this robot object do?), and a main loop (listen, think, speak, repeat).
  - Who touches what in our Python script: modifying intelligence (`_ask_claude()`, emotion, memory, future policy or model); observing data (DDS messages, API requests, logs, conversation history); making it run (network interface, dependencies, Unitree SDK, DDS, deployment).
- C++: low-level robot control. Unitree's example `g1_ankle_swing_example` uses DDS publisher and subscriber classes, low-level command and state messages, and control threads that run every 2 milliseconds. It defines 29 motor slots (hip, knee, ankle, waist, shoulder, elbow, wrist) and stores target position, target velocity, stiffness, damping and feed-forward torque for each.
  - Warning from class: low-level C++ code can directly affect joints and physical motion. Understand the state, command, timing and safety conditions before running or changing it on the real robot.
  - Concepts: `#include`, type declarations, constants (`G1_NUM_MOTOR = 29`), arrays, `struct`, `class`, functions, pointers and references, threads.
- C#: used in Lucky Engine to organize and run simulated behavior. Lucky Engine creates virtual worlds where a robot can try, fail and learn many times before stepping into reality: no hardware to break and no lab time to book. Example: record episodes of observations and actions, build a dataset, train a policy, and run the policy in simulation.
  - Concepts: class, variable, boolean, function, `if`, `switch`, `enum`, array.
- Python is often used to build intelligence; C# here is used to organize and run the simulated behavior.

## Safety: slow down or speed up?
- Key takeaway from the AI safety discussion: a capable agent may optimize for the task we give it, not for the safety boundaries we assume.
- Our lab development pipeline: move fast in simulation, move carefully in reality.
  - New behavior begins in Lucky Engine or MuJoCo.
  - Record observations, actions, failures and successes.
  - Evaluate before deployment.
  - Require human review before testing on the real G1.
  - Start real-robot testing with limited actions and reduced authority.
  - Keep an independent human stop path.
- Unsafe: Claude writing straight to `rt/lowcmd` on the G1. Preferred: Claude or a policy proposes "wave" -> allowed action -> safety check -> tested controller -> G1.
- "AI proposes. Software validates. Robot executes." The AI should not be able to override physical limits.
- Three teams, three responsibilities: what should the robot learn (success, collision, timeout and unexpected-action rates), what do we let it run (command limits, interfaces, deployment boundaries: who issued the command, when, which process, which DDS topic, which robot, was it allowed), and what actually happened (logging, traffic, anomaly detection, dashboard).

## Lab report grading (5 points)
Progress toward the final project 2, technical evidence 1, explanation or reflection 1, next-step plan 1.

# Week 3 (Sep 9; Sep 7 was Labor Day): How do robots communicate?

Learning objectives: explain the roles of ROS 2 and DDS; identify a publisher, subscriber, topic and message; describe how publish-subscribe supports robot communication; follow the safety and calibration procedures for the Unitree G1. Reading: the Unitree DDS Services Interface.

## ROS 2 and DDS
- ROS 2 is a software framework that helps separate robot programs work together. It runs on an operating system such as Linux.
  - Nodes: programs with one job, such as listening, recognizing speech, planning movement or monitoring sensors.
  - Topics: carry ongoing updates, such as camera images and robot state.
  - Services: a request and a reply, such as asking for a status check.
  - Actions: longer tasks with progress updates and cancellation.
  - Tools: launch programs, inspect messages, visualize data and record experiments.
- ROS 2 organizes the work; DDS commonly handles the underlying data exchange.
- Analogy from class: Linux provides the school building, ROS 2 organizes the teachers and their jobs, and DDS carries their messages.
- For our G1 we can use Unitree SDK 2 directly, without ROS 2. ROS 2 becomes useful when we want to organize and connect several parts of the teaching assistant.

## DDS: how robot programs exchange data
- DDS means Data Distribution Service: middleware that lets programs share data through a publish-subscribe model.
- Data writer: publishes data, such as the G1's joint angles or body orientation.
- Topic: a named channel for one type of message.
- Data reader: subscribes to a topic to receive its updates.
- DDS domain: groups the participants that can discover and talk to each other.
- Quality of Service (QoS): the communication rules, such as reliability and how much history to keep.
- Filter: selects relevant updates by their content.
- G1 example: the G1 publishes its robot state, our Python program subscribes, and the classroom dashboard displays the readings.
- Cyclone DDS is open source, so using it does not require buying a commercial DDS product.

## Teams and final project deliverables
There are three teams with three missions:
- Security team ("officers"): monitor all robot network connections and ports to understand traffic and identify security risks. Deliverable: a network monitoring dashboard for CGI-112 with a network map of approved devices, traffic monitoring (ports, endpoints, volume, failures), teaching-assistant monitoring (API request counts, response times and errors, without recording API keys or private conversations), security alerts for unfamiliar devices or unusual traffic, a live dashboard, and a final report.
- Simulation team ("researchers"): apply physics-based simulation to test ideas. Deliverable: simulate the G1 as a teaching assistant in a simplified CGI-112 scene and test a physical classroom task (reposition a chair, carry a light object, or point toward a teaching area), with experiments on weight, friction and placement, a results dashboard and a final report. Physical robot trials require instructor supervision and a safety review.
- Deploy team ("engineers"): build Linux and deployment skills to create reliable human-robot interaction. Deliverable: deploy the G1 as a teaching assistant in CGI-112 that can listen, respond and support classroom interaction: Linux environment, conversation system (microphone, speech recognition, AI model, speaker), teaching support using instructor-provided course materials, approved gestures under supervision, system monitoring shared with the security team, and a final demonstration and report.

## G1 calibration activity
Goal: check the G1's posture and decide whether an approved calibration or offset adjustment improves an observed lean.
- Observe: does the G1 lean to one side while standing or walking?
- Understand: calibration checks reference values; an offset applies a correction. A lean alone does not tell us the cause.
- Prepare: check the floor, clear the area, and confirm the required robot mode with the instructor.
- Adjust: follow the manufacturer's procedure under instructor or TA supervision.
- Verify: compare posture, walking and IMU readings before and after.
- Document: record the setting changed, the observations and whether it improved.

## "See or touch?": tactile sensing
Optical tactile sensing uses a camera to detect touch: a soft surface bends when an object presses on it, lights illuminate the surface from inside the sensor, a camera records how the surface changes shape, and software analyzes those changes to work out where and how contact occurs. Papers mentioned: Tabero (gentle manipulation with closed-loop force feedback from vision, touch and language) and TacSL (a library for visuotactile sensor simulation and learning).

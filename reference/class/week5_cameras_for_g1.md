# Week 5 (Sep 21 and 23): How do we choose and mount cameras for the G1?

Essential question: how should we choose and position cameras for Yotie so it can see people, navigate around desks, and observe objects?

## What the code does today
`teleop/face_id.py` and `scripts/enroll_face.py` implement a full face pipeline: enroll one photo per person, match live frames against it, label a live OpenCV window, and pass the matched name to `MemoryManager.build_context()` so Claude knows who it is talking to. But the whole pipeline runs on `cv2.VideoCapture(camera_index=0)`, the laptop's own webcam. It has never used the G1's head camera. "Face recognition for the G1" and "face recognition on my laptop" are currently the same code and should not be treated as interchangeable.

## The G1's camera: Intel RealSense D435i
The G1's depth camera is mounted overhead in the head. It contains two infrared cameras (global shutter), an infrared laser projector, an RGB camera (rolling shutter) and a 6-axis IMU.
- Depth: stereoscopic; ideal range 0.3 to 3 m; minimum distance about 28 cm at full resolution; accuracy better than 2% at 2 m; field of view 87 x 58 degrees; up to 1280 x 720; up to 90 frames per second.
- RGB: 1920 x 1080, about 2 megapixels; 30 frames per second; rolling shutter; field of view 69 x 42 degrees.
- For face recognition, those 2 million pixels cover the whole scene, not each face. A distant student's face occupies few pixels, so it holds less detail. Enlarging the crop cannot recover detail the camera never captured.

## What you need to know about the camera
1. Inside the D435i: what each component measures.
2. Stereo depth: baseline, matching image features, disparity and depth. This is how distance to a chair or obstacle is estimated; stereo cameras calculate distance using disparity.
3. RGB versus depth: color pixels describe appearance; depth pixels describe distance. Detecting an object is different from locating it.
4. Time and motion: frame rate, exposure, global versus rolling shutter, latency. Moving scenes can give unreliable observations.
5. Installation geometry: camera height, tilt, field of view and blind spots decide whether people, desks and chair legs are in view.
6. Calibration and alignment: camera intrinsics, RGB-depth alignment, and camera-to-robot transforms, which convert an observed point into a location relative to Yotie.
7. Limitations and validation: missing depth, occlusion, reflective surfaces, small objects and changing lighting. Decide when an observation is not good enough to move on.

## Human eyes versus the D435i
- Human vision, both eyes: about 200 x 135 degrees; the binocular overlap is about 120 x 130 degrees.
- D435i depth camera: 87 x 58 degrees. D435i RGB camera: 69 x 42 degrees.
- Class question: why not simply choose a wide-view camera?
- In a class test with the Unitree app's camera view, pushing a chair looked doable if we know the elbow rotation angle and when to rotate the elbow.

## A second camera: RealSense D405
The D435i is mainly for observing the environment; the D405 is optimized for close hand-object interaction. For Yotie it could measure the distance between the wrist, fingers and an object during grasping. Its 7 to 50 cm working range makes it unsuitable as the only camera for seeing people, desks or chairs across the classroom.
- Stereo RGB-D; depth field of view 87 x 58 degrees; up to 1280 x 720; 30 frames per second, up to 90 at lower resolution; global shutter; 18 mm stereo baseline.
- No IMU and no infrared projector, so it needs good lighting and the robot's pose data.
- 42 x 42 x 23 mm, about 60 g, small enough for the G1's wrist. USB 3.1, about 1.55 W: needs secure cabling and enough USB bandwidth.
- Mounting references: Unitree's `xr_teleoperate` repository (Device.md) and Unitree's RoboCup page.

## Paper of the week: Zero-WAM
Zero-WAM predicts future robot videos and executable actions from a human video prompt or a language instruction, and is tested on unseen simulated tasks and real-world tasks. Reminder from class: most arXiv papers are preprints, shared before or during formal peer review.

## Lab 2 camera add-on, by team
- Simulation team: open a camera view of what the G1 can observe in Lucky Engine, and test whether it can see the whole chair or its relevant parts, the chair's position relative to the G1, and nearby obstacles.
- Security team: investigate how camera data affects the G1's network architecture, bandwidth, privacy and security monitoring, and whether the stream stays inside the G1, goes to a local workstation or crosses the campus network.
- Deploy team: consider how camera data will enter the G1 software and control pipeline, how to modify the head-mounted camera or add wrist cameras, and 3D-printed camera parts.

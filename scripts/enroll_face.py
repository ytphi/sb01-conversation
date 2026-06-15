#!/usr/bin/env python3
"""
Enroll your face for G1 recognition.
Usage: python3 scripts/enroll_face.py <your_name>
"""
import sys
import os

try:
    import cv2
    import face_recognition
except ImportError:
    print("Install dependencies first:  pip install face-recognition opencv-python")
    sys.exit(1)

FACES_DIR = os.path.join(os.path.dirname(__file__), "../memory/faces")


def enroll(name: str, camera_index: int = 0):
    os.makedirs(FACES_DIR, exist_ok=True)
    cap = cv2.VideoCapture(camera_index)
    if not cap.isOpened():
        print("Could not open camera")
        sys.exit(1)

    print(f"Enrolling: {name}")
    print("Look at the camera — press SPACE to capture, ESC to cancel.")

    while True:
        ret, frame = cap.read()
        if not ret:
            continue

        # draw a guide box
        h, w = frame.shape[:2]
        cx, cy = w // 2, h // 2
        cv2.rectangle(frame, (cx - 100, cy - 120), (cx + 100, cy + 120), (0, 255, 0), 2)
        cv2.putText(frame, "Center your face — SPACE to capture",
                    (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        cv2.imshow("Face Enrollment", frame)

        key = cv2.waitKey(1)
        if key == 27:
            print("Cancelled.")
            break
        elif key == 32:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            encodings = face_recognition.face_encodings(rgb)
            if not encodings:
                print("No face detected — try again.")
                continue
            path = os.path.join(FACES_DIR, f"{name.lower()}.jpg")
            cv2.imwrite(path, frame)
            print(f"Enrolled {name} → {path}")
            break

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/enroll_face.py <your_name>")
        sys.exit(1)
    enroll(sys.argv[1], camera_index=int(sys.argv[2]) if len(sys.argv) > 2 else 0)

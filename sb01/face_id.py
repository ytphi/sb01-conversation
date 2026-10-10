import os
import threading

_HERE = os.path.dirname(__file__)
FACES_DIR = os.path.abspath(os.path.join(_HERE, "../memory/faces"))

try:
    import cv2
    import face_recognition
    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False


class FaceIdentifier:
    def __init__(self):
        self.known_encodings = []
        self.known_names = []
        self._window_running = False
        if _AVAILABLE:
            self._load_enrolled()

    def _load_enrolled(self):
        os.makedirs(FACES_DIR, exist_ok=True)
        for filename in sorted(os.listdir(FACES_DIR)):
            if not filename.lower().endswith((".jpg", ".jpeg", ".png")):
                continue
            name = os.path.splitext(filename)[0]
            img = face_recognition.load_image_file(os.path.join(FACES_DIR, filename))
            encodings = face_recognition.face_encodings(img)
            if encodings:
                self.known_encodings.append(encodings[0])
                self.known_names.append(name)
        print(f"[face_id] enrolled: {self.known_names or 'none'}")

    def identify(self, camera_index: int = 0, attempts: int = 10, tolerance: float = 0.5) -> str | None:
        """Capture frames from camera and return matched name, or None if unknown."""
        if not _AVAILABLE:
            print("[face_id] face_recognition not installed — skipping")
            return None
        if not self.known_encodings:
            print("[face_id] no enrolled faces — skipping")
            return None

        cap = cv2.VideoCapture(camera_index)
        if not cap.isOpened():
            print("[face_id] cannot open camera")
            return None

        # discard first frames while camera warms up
        for _ in range(15):
            cap.read()

        matched = None
        for _ in range(attempts):
            ret, frame = cap.read()
            if not ret:
                continue
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            locations = face_recognition.face_locations(rgb)
            encodings = face_recognition.face_encodings(rgb, locations)
            for enc in encodings:
                matches = face_recognition.compare_faces(
                    self.known_encodings, enc, tolerance=tolerance
                )
                if True in matches:
                    matched = self.known_names[matches.index(True)]
                    break
            if matched:
                break

        cap.release()
        return matched

    def start_live_window(self, camera_index: int = 0, tolerance: float = 0.5):
        """Open a persistent camera window with face bounding boxes and name labels."""
        if not _AVAILABLE:
            return
        self._window_running = True
        t = threading.Thread(
            target=self._window_loop,
            args=(camera_index, tolerance),
            daemon=True,
        )
        t.start()

    def stop_live_window(self):
        self._window_running = False

    def _window_loop(self, camera_index: int, tolerance: float):
        cap = cv2.VideoCapture(camera_index)
        if not cap.isOpened():
            print("[face_id] cannot open camera for live window")
            return

        cv2.namedWindow("sb01 — Face Recognition", cv2.WINDOW_NORMAL)

        while self._window_running:
            ret, frame = cap.read()
            if not ret:
                continue

            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            locations = face_recognition.face_locations(rgb)
            encodings = face_recognition.face_encodings(rgb, locations)

            for (top, right, bottom, left), enc in zip(locations, encodings):
                name = "Unknown"
                if self.known_encodings:
                    matches = face_recognition.compare_faces(
                        self.known_encodings, enc, tolerance=tolerance
                    )
                    if True in matches:
                        name = self.known_names[matches.index(True)]

                cv2.rectangle(frame, (left, top), (right, bottom), (0, 255, 0), 2)
                cv2.rectangle(frame, (left, bottom - 30), (right, bottom), (0, 255, 0), cv2.FILLED)
                cv2.putText(
                    frame, name, (left + 6, bottom - 8),
                    cv2.FONT_HERSHEY_DUPLEX, 0.7, (0, 0, 0), 1,
                )

            cv2.imshow("sb01 — Face Recognition", frame)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

        cap.release()
        cv2.destroyWindow("sb01 — Face Recognition")

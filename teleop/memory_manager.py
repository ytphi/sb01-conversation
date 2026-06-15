import json
import os
from datetime import datetime

_HERE = os.path.dirname(__file__)
MEMORY_DIR = os.path.abspath(os.path.join(_HERE, "../memory"))


class MemoryManager:
    def __init__(self):
        self._ensure_dirs()

    def _ensure_dirs(self):
        for sub in ("faces", "profiles", "sessions"):
            os.makedirs(os.path.join(MEMORY_DIR, sub), exist_ok=True)

    # ── profile ──────────────────────────────────────────────────────────────

    def _profile_path(self, name: str) -> str:
        return os.path.join(MEMORY_DIR, "profiles", f"{name.lower()}.json")

    def load_profile(self, name: str) -> dict:
        path = self._profile_path(name)
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
        return {"name": name, "facts": [], "preferences": []}

    def save_profile(self, name: str, profile: dict):
        with open(self._profile_path(name), "w") as f:
            json.dump(profile, f, indent=2)

    def add_facts(self, name: str, new_facts: list[str]):
        profile = self.load_profile(name)
        existing = set(profile.get("facts", []))
        profile["facts"] = list(existing | set(new_facts))
        self.save_profile(name, profile)

    # ── sessions ─────────────────────────────────────────────────────────────

    def _sessions_dir(self, name: str) -> str:
        path = os.path.join(MEMORY_DIR, "sessions", name.lower())
        os.makedirs(path, exist_ok=True)
        return path

    def load_recent_sessions(self, name: str, n: int = 3) -> str:
        sdir = self._sessions_dir(name)
        files = sorted(f for f in os.listdir(sdir) if f.endswith(".txt"))[-n:]
        parts = []
        for fname in files:
            with open(os.path.join(sdir, fname)) as f:
                date_label = fname.replace(".txt", "").replace("_", " ")
                parts.append(f"[{date_label}] {f.read().strip()}")
        return "\n".join(parts)

    def save_session_summary(self, name: str, summary: str):
        sdir = self._sessions_dir(name)
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
        with open(os.path.join(sdir, f"{stamp}.txt"), "w") as f:
            f.write(summary)

    # ── context builder ───────────────────────────────────────────────────────

    def build_context(self, name: str) -> str:
        profile = self.load_profile(name)
        sessions = self.load_recent_sessions(name)

        lines = [f"The person talking to you is {profile.get('name', name)}."]

        if profile.get("facts"):
            lines.append("Known facts about them: " + "; ".join(profile["facts"]) + ".")

        if profile.get("preferences"):
            lines.append("Their preferences: " + "; ".join(profile["preferences"]) + ".")

        if sessions:
            lines.append("Recent conversation summaries:\n" + sessions)

        return "\n".join(lines)

#!/usr/bin/env python3
"""
import_course_zip.py  -  pull course documents out of a Google Drive export for Yotie

Extracts only the files Yotie's knowledge base can read (.pdf .pptx .docx .xlsx) into
memory/course_files/, keeping the Drive folder layout. Skips 3D models, videos, student
records (rosters, sign-ups, progress sheets — see COURSE_FILE_EXCLUDE in
teleop/knowledge_base.py), and readings already in memory/papers/.
Yotie re-indexes on its next start.

Usage:
  python3 scripts/import_course_zip.py ~/Downloads/IST5930_Fall2026-*.zip
"""

import os
import sys
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from teleop.knowledge_base import COURSE_FILES_DIR, COURSE_FILE_TYPES, COURSE_FILE_EXCLUDE, MEMORY_DIR

SKIP_DIRS = ("3D Models/", "/video/")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    papers = set(os.listdir(os.path.join(MEMORY_DIR, "papers")))
    dest = os.path.abspath(COURSE_FILES_DIR)
    for zpath in sys.argv[1:]:
        with zipfile.ZipFile(os.path.expanduser(zpath)) as z:
            for info in z.infolist():
                name = info.filename
                base = os.path.basename(name)
                if (info.is_dir() or not base.lower().endswith(COURSE_FILE_TYPES)
                        or any(d in name for d in SKIP_DIRS)
                        or any(x.lower() in base.lower() for x in COURSE_FILE_EXCLUDE)):
                    continue
                if base in papers:
                    print(f"skip (already in memory/papers): {base}")
                    continue
                rel = name.split("/", 1)[1] if "/" in name else name   # drop the export's top folder
                out = os.path.normpath(os.path.join(dest, rel.replace(" /", "/")))
                if not out.startswith(dest + os.sep):                  # no path traversal
                    print(f"skip (unsafe path): {name}")
                    continue
                os.makedirs(os.path.dirname(out), exist_ok=True)
                with z.open(info) as src, open(out, "wb") as dst:
                    dst.write(src.read())
                print(f"extracted: {os.path.relpath(out, dest)}")
    print(f"\ndone → {dest}  (Yotie re-indexes on next start)")


if __name__ == "__main__":
    main()

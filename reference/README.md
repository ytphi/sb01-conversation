# Reference material for Yotie

## `class/` — course material

Every `.md` or `.txt` file in `reference/class/` is read when `scripts/sb01_conversation.py` starts, in
name order, and given to Claude with each question, so Yotie can answer as the teaching assistant for
the course. The startup output lists each file and how much of it was used.

- **To add a week:** put one more text file in the folder, for example `week7_how_robots_remember.md`,
  and restart the conversation program. No code change is needed.
- **Plain text only.** PDFs and slide decks are not read; put their content into a `.md` or `.txt` file.
- **Keep files under 8,000 characters.** Anything past that is left out, and the startup line says so.
  Everything is sent with every question, so shorter notes also mean quicker replies.
- **This repository is public.** Leave out students' names, contact details and anything else that
  should not be published.
- `SB01_CLASS_DIR=/some/folder` uses another folder instead. An empty or missing folder just means
  Yotie has no course material.

The current files are notes condensed from the IST 5930 (Fall 2026) syllabus and the lecture slides
for weeks 1 to 6. They are summaries, not the slides word for word: if a slide changes, update its file.
Claude is told to say it is not sure, and to point to Canvas or the instructor, when the material does
not cover a question about the class.

`tests/test_class_material.py` checks the loading and that the files here hold no email addresses,
phone numbers or team rosters.

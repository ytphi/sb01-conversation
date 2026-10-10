import glob
import logging
import os
import pickle
import re

_HERE = os.path.dirname(__file__)
MEMORY_DIR = os.path.abspath(os.path.join(_HERE, "../memory"))
KB_SUBDIRS = ["lectures", "papers", "profiles"]
CACHE_PATH = os.path.join(MEMORY_DIR, ".knowledge_cache.pkl")

# Markdown course notes (weekly Canvas pages, lab handouts, lecture notes) are indexed in
# place from the instructor's Obsidian vault, so edits show up on the next run. Paths that
# don't exist (e.g. on a student machine) are skipped.
NOTE_SOURCES = [
    "~/Documents/Notes/Work/Teaching/IST5930",
    "~/Documents/Notes/Work/Teaching/15-Week Lab Practice Guide.md",
    "~/Documents/Notes/Study/Readings/Embodied AI Readings.md",
]
NOTE_EXCLUDE = {
    "2024Fall IST5930.md",                 # a different course (LLM security)
    "Fall2026 IST5930 Logistics Prep.md",  # instructor to-do list
    "Lab 4 — How Yotie Remembers People (Canvas).md",  # draft; Lab 4 not decided yet
}
# Course files exported from Google Drive (slides, lab docs, schedule sheets), searched
# recursively. Class-only like the notes: they can mention students.
COURSE_FILES_DIR = os.path.join(MEMORY_DIR, "course_files")
COURSE_FILE_TYPES = (".pdf", ".pptx", ".docx", ".xlsx", ".md")
# Final Canvas pages (as posted to students) go in course_files/Canvas/ as .md. A final can
# name the Obsidian draft it supersedes in frontmatter ("replaces: <draft filename>.md");
# that draft is then left out of the index.
CANVAS_FINAL_DIR = os.path.join(COURSE_FILES_DIR, "Canvas")
# Never index student records, even if a full Drive export is dropped in.
COURSE_FILE_EXCLUDE = ("Lab Sheets", "Volunteer_Signup", "Signup", "Roster", "Grades", "Attendance",
                       "IST5930_2026_Template")
# Course files with no student names: also searchable in demo mode (visitors). Slide decks,
# lab docs and the Obsidian pages include team rosters, so they stay class-only.
COURSE_FILE_PUBLIC = ("/Reading/", "CourseSchedule", "CSUSB_IST5930_Fall2026.docx")
INDEX_VERSION = 9   # bump to force a rebuild when indexing logic changes

CHUNK_SIZE = 900
CHUNK_OVERLAP = 150
# Markdown sections (one Canvas team deliverable, one lab step) stay whole up to this size,
# so a deliverables list isn't cut in half.
NOTE_CHUNK_SIZE = 1600
TOP_K = 3
MIN_SCORE = 0.12

# pypdf logs harmless "could not convert string to float" warnings for oddly formatted PDFs
logging.getLogger("pypdf").setLevel(logging.ERROR)

try:
    from pypdf import PdfReader
    _PDF_AVAILABLE = True
except ImportError:
    _PDF_AVAILABLE = False

try:
    from pptx import Presentation
    from docx import Document
    from openpyxl import load_workbook
    _OFFICE_AVAILABLE = True
except ImportError:
    _OFFICE_AVAILABLE = False

try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    import numpy as np
    _SKLEARN_AVAILABLE = True
except ImportError:
    _SKLEARN_AVAILABLE = False


# "week 7" / "Lab 4" / "Team 3" → "week7" / "lab4" / "team3", in both the index and the
# query. Otherwise the tokenizer drops the lone digit and every lab page looks the same.
# Underscores become spaces first, so file titles like "Lab2_..._Team 3" split into words.
# Speech-to-text often spells numbers out ("week seven"), so number words count too.
_NUMBER_WORDS = {w: str(i) for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen twenty".split())}
_NUMBERED = re.compile(r"\b(week|lab|lecture|module|lesson|team|group)\s*[-#]?\s*(\d+|"
                       + "|".join(_NUMBER_WORDS) + r")\b", re.IGNORECASE)
_NUMBERED_TERM = re.compile(r"(?:week|lab|lecture|module|lesson|team|group)\d+")


def _preprocess(text: str) -> str:
    return _NUMBERED.sub(lambda m: f"{m.group(1)}{_NUMBER_WORDS.get(m.group(2), m.group(2))}",
                         text.lower().replace("_", " "))


def _title_terms(chunk: dict) -> list[str]:
    """Numbered terms in a chunk's file title / section label (e.g. the 'Week7' deck, the
    'Week 7 10/05' schedule row) — the chunk is *about* that week, not just mentioning it."""
    return sorted(set(_NUMBERED_TERM.findall(_preprocess(f"{chunk['source']} {chunk.get('section', '')}"))))


def _replaced_drafts() -> set[str]:
    """Draft filenames named in Canvas finals' frontmatter ('replaces: …')."""
    replaced = set()
    for path in glob.glob(os.path.join(CANVAS_FINAL_DIR, "*.md")):
        with open(path, encoding="utf-8") as f:
            head = f.read(2000)
        m = re.match(r"---\n(.*?)\n---\n", head, re.DOTALL)
        if m:
            replaced.update(v.strip() for v in re.findall(r"^replaces:\s*(.+)$", m.group(1), re.MULTILINE))
    return replaced


def _note_paths() -> list[str]:
    replaced = _replaced_drafts()
    paths = []
    for src in NOTE_SOURCES:
        src = os.path.expanduser(src)
        if os.path.isdir(src):
            paths.extend(sorted(glob.glob(os.path.join(src, "*.md"))))
        elif os.path.isfile(src):
            paths.append(src)
    return [p for p in paths if os.path.basename(p) not in NOTE_EXCLUDE | replaced]


def _clean_markdown(text: str) -> str:
    """Strip Obsidian syntax and instructor-only 'Check before posting' callouts."""
    text = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.DOTALL)       # frontmatter
    kept, skipping = [], False
    for line in text.splitlines():
        if re.match(r"^>\s*\[!\w+\][-+]?\s*Check before posting", line, re.IGNORECASE):
            skipping = True
            continue
        if skipping and line.startswith(">"):
            continue
        skipping = False
        kept.append(line)
    text = "\n".join(kept)
    text = re.sub(r"!\[\[[^\]]*\]\]", "", text)                           # embeds
    text = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", text)                # [[target|alias]]
    text = re.sub(r"\[\[([^\]]*)\]\]", r"\1", text)                        # [[target]]
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)                   # [text](url)
    return text


def _chunk_note(path: str) -> list[dict]:
    """Split a markdown note into heading sections, then into overlapping windows.
    Each chunk starts with 'note title — section:' so the title words are searchable."""
    title = os.path.splitext(os.path.basename(path))[0]
    try:
        with open(path, encoding="utf-8") as f:
            text = _clean_markdown(f.read())
    except Exception as exc:
        print(f"[knowledge_base] could not read {os.path.basename(path)}: {exc}")
        return []

    sections, heading, lines = [], title, []
    for line in text.splitlines():
        m = re.match(r"^#{1,6}\s+(.*)", line)
        if m:
            sections.append((heading, lines))
            heading, lines = m.group(1).strip(), []
        else:
            lines.append(line)
    sections.append((heading, lines))

    chunks = []
    for heading, body in sections:
        body = re.sub(r"\s+", " ", " ".join(body)).strip()
        if not body:
            continue
        label = title if heading == title else f"{title} — {heading}"
        start = 0
        while start < len(body):
            piece = body[start:start + NOTE_CHUNK_SIZE].strip()
            if piece:
                chunks.append({"source": title, "section": heading, "kind": "note",
                               "text": f"{label}: {piece}"})
            start += NOTE_CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


def _course_file_paths() -> list[str]:
    paths = []
    for path in sorted(glob.glob(os.path.join(COURSE_FILES_DIR, "**", "*"), recursive=True)):
        name = os.path.basename(path)
        if (path.lower().endswith(COURSE_FILE_TYPES) and not name.startswith("~$")
                and not any(x.lower() in name.lower() for x in COURSE_FILE_EXCLUDE)):
            if path.lower().endswith((".pdf", ".md")) or _OFFICE_AVAILABLE:
                paths.append(path)
    return paths


def _extract_units(path: str) -> list[tuple[str, str]]:
    """(location label, text) units for a course file: PDF pages, slides (with speaker
    notes), Word sections, or spreadsheet rows."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".pdf":
            return [(f"p.{i}", t) for i, t in enumerate(_extract_pages(path), start=1)]
        if ext == ".pptx":
            units = []
            for i, slide in enumerate(Presentation(path).slides, start=1):
                texts = [p.text for shape in slide.shapes if shape.has_text_frame
                         for p in shape.text_frame.paragraphs if p.text.strip()]
                for shape in slide.shapes:
                    if shape.has_table:
                        texts += [" | ".join(c.text for c in row.cells) for row in shape.table.rows]
                if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
                    texts.append("Speaker notes: " + slide.notes_slide.notes_text_frame.text)
                units.append((f"slide {i}", "\n".join(texts)))
            return units
        if ext == ".docx":
            units, heading, lines = [], "", []
            for para in Document(path).paragraphs:
                if para.style.name.lower().startswith("heading") and para.text.strip():
                    units.append((heading, "\n".join(lines)))
                    heading, lines = para.text.strip(), []
                elif para.text.strip():
                    lines.append(para.text)
            units.append((heading, "\n".join(lines)))
            return [(h or "start", t) for h, t in units if t.strip()]
        if ext == ".xlsx":
            units = []
            for ws in load_workbook(path, read_only=True, data_only=True).worksheets:
                header = None
                for r, row in enumerate(ws.iter_rows(values_only=True), start=1):
                    cells = ["" if c is None else str(c).strip() for c in row]
                    if not any(cells):
                        continue
                    if header is None:
                        header = cells
                        continue
                    pairs = [f"{h}: {c}" if h else c for h, c in zip(header, cells) if c]
                    label = cells[0][:40] if cells[0] else f"row {r}"   # e.g. "Week 7 10/05"
                    units.append((f"{ws.title}: {label}", " | ".join(pairs)))
            return units
    except Exception as exc:
        print(f"[knowledge_base] could not read {os.path.basename(path)}: {exc}")
    return []


def _chunk_course_file(path: str) -> list[dict]:
    """Chunk a course file; each chunk starts with 'file title — location:' so the title
    words (e.g. 'Week5') are searchable."""
    title = os.path.splitext(os.path.basename(path))[0]
    kind = "public" if any(p in path for p in COURSE_FILE_PUBLIC) else "note"
    chunks = []
    for loc, raw in _extract_units(path):
        text = re.sub(r"\s+", " ", raw).strip()
        start = 0
        while start < len(text):
            piece = text[start:start + CHUNK_SIZE].strip()
            if piece:
                chunks.append({"source": title, "section": loc, "kind": kind,
                               "text": f"{title} — {loc}: {piece}"})
            start += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


def _pdf_paths() -> list[str]:
    paths = []
    for sub in KB_SUBDIRS:
        d = os.path.join(MEMORY_DIR, sub)
        if os.path.isdir(d):
            paths.extend(sorted(glob.glob(os.path.join(d, "*.pdf"))))
    return paths


def _extract_pages(path: str) -> list[str]:
    try:
        reader = PdfReader(path)
        return [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        print(f"[knowledge_base] could not read {os.path.basename(path)}: {exc}")
        return []


def _chunk_pages(pages: list[str], source: str) -> list[dict]:
    chunks = []
    for page_num, raw in enumerate(pages, start=1):
        text = re.sub(r"\s+", " ", raw).strip()
        start = 0
        while start < len(text):
            end = start + CHUNK_SIZE
            piece = text[start:end].strip()
            if piece:
                chunks.append({"source": source, "page": page_num, "text": piece})
            start = end - CHUNK_OVERLAP
    return chunks


class KnowledgeBase:
    """TF-IDF search over PDFs in memory/lectures, memory/papers, memory/profiles, plus the
    markdown course notes in NOTE_SOURCES.

    Lets students ask sb01 about lecture slides, weekly pages, lab instructions, assigned
    readings, and the instructor's CV. Rebuilds its cache only when the set of files changes.
    """

    def __init__(self):
        self.chunks: list[dict] = []
        self.vectorizer = None
        self.matrix = None
        self.available = _PDF_AVAILABLE and _SKLEARN_AVAILABLE
        if not _PDF_AVAILABLE:
            print("[knowledge_base] pypdf not installed — knowledge base disabled")
        elif not _SKLEARN_AVAILABLE:
            print("[knowledge_base] scikit-learn not installed — knowledge base disabled")
        else:
            self._load_or_build()

    def _fingerprint(self, paths: list[str]):
        # scikit-learn version included: a pickled vectorizer from another version can break
        import sklearn
        return [INDEX_VERSION, sklearn.__version__] + sorted(
            (os.path.basename(p), os.path.getmtime(p), os.path.getsize(p))
            for p in paths
        )

    def _load_or_build(self):
        pdf_paths, note_paths, course_paths = _pdf_paths(), _note_paths(), _course_file_paths()
        paths = pdf_paths + note_paths + course_paths
        if not paths:
            print("[knowledge_base] no PDFs or course notes found")
            return
        fingerprint = self._fingerprint(paths)

        if os.path.exists(CACHE_PATH):
            try:
                with open(CACHE_PATH, "rb") as f:
                    cached = pickle.load(f)
                if cached.get("fingerprint") == fingerprint:
                    self.chunks = cached["chunks"]
                    self.vectorizer = cached["vectorizer"]
                    self.matrix = cached["matrix"]
                    print(f"[knowledge_base] loaded cached index "
                          f"({len(self.chunks)} chunks, {len(paths)} docs)")
                    return
            except Exception as exc:
                print(f"[knowledge_base] cache load failed, rebuilding: {exc}")

        print(f"[knowledge_base] indexing {len(pdf_paths)} PDFs + {len(note_paths)} course notes + "
              f"{len(course_paths)} course files — files changed, rebuilding the index "
              f"(~30s, don't interrupt; next start is instant)...")
        chunks = []
        for path in pdf_paths:
            pages = _extract_pages(path)
            chunks.extend(_chunk_pages(pages, os.path.basename(path)))
        for path in note_paths:
            chunks.extend(_chunk_note(path))
        for path in course_paths:
            print(f"[knowledge_base]   {os.path.basename(path)}")
            chunks.extend(_chunk_note(path) if path.endswith(".md") else _chunk_course_file(path))

        if not chunks:
            print("[knowledge_base] no extractable text found")
            return
        finals = {os.path.splitext(os.path.basename(p))[0] for p in course_paths
                  if os.path.dirname(p) == CANVAS_FINAL_DIR}
        for c in chunks:
            c["title_terms"] = _title_terms(c)
            c["canvas_final"] = c["source"] in finals

        vectorizer = TfidfVectorizer(stop_words="english", max_df=0.9, preprocessor=_preprocess)
        matrix = vectorizer.fit_transform([c["text"] for c in chunks])

        self.chunks, self.vectorizer, self.matrix = chunks, vectorizer, matrix

        try:
            with open(CACHE_PATH, "wb") as f:
                pickle.dump({
                    "fingerprint": fingerprint,
                    "chunks": chunks,
                    "vectorizer": vectorizer,
                    "matrix": matrix,
                }, f)
        except Exception as exc:
            print(f"[knowledge_base] could not write cache: {exc}")

        print(f"[knowledge_base] indexed {len(chunks)} chunks from {len(paths)} documents")

    def retrieve(self, query: str, k: int = TOP_K, min_score: float = MIN_SCORE,
                 include_notes: bool = True) -> str:
        """Return the top-k relevant snippets for `query`, or '' if nothing is relevant.

        include_notes=False skips class-only material — the Obsidian pages, slide decks and
        lab docs name students — leaving PDFs, readings, the schedule and course description
        (for visitor demos).

        Requires at least 2 shared vocabulary terms (not just a high cosine score) so a
        single rare word in common — e.g. a place name that also appears in a paper's
        bibliography — can't masquerade as a real topical match.

        Exception: a numbered term in the query ("week3", "lab4", "team2") pins the search
        to chunks containing it, and that one term is enough. Otherwise "what did we learn
        in week 3?" shares only "week3" with the Week 3 slides and gets filtered out. Pinned
        results rank chunks matching more numbered terms first (Team 3 + Lab 2 beats either
        alone), then final Canvas pages (the official version), then chunks whose title/section *is* that week (the Week 7 deck, the "Week 7"
        schedule row) above passing mentions; at most 2 per file, two extra snippets, so the
        schedule row for that week isn't crowded out by one slide deck.
        """
        if not self.available or self.matrix is None or not query.strip():
            return ""
        analyzer = self.vectorizer.build_analyzer()
        query_terms = set(analyzer(query))
        if not query_terms:
            return ""
        min_shared = min(2, len(query_terms))

        q_vec = self.vectorizer.transform([query])
        scores = cosine_similarity(q_vec, self.matrix)[0]

        cols = [self.vectorizer.vocabulary_[t] for t in query_terms
                if _NUMBERED_TERM.fullmatch(t) and t in self.vectorizer.vocabulary_]
        per_source = k
        if cols:
            numbered = {t for t in query_terms if _NUMBERED_TERM.fullmatch(t)}
            n_terms = np.asarray((self.matrix[:, cols] > 0).sum(axis=1)).ravel()
            in_title = np.array([len(numbered.intersection(c.get("title_terms", ()))) for c in self.chunks])
            official = np.array([0.5 if c.get("canvas_final") else 0.0 for c in self.chunks])
            scores = np.where(n_terms > 0, scores + n_terms + in_title + official, -1.0)
            min_shared, min_score, k, per_source = 1, 0.0, k + 2, 2

        # over-fetch, then filter
        top_idx = scores.argsort()[::-1][:k * 10]
        parts, seen, from_source = [], set(), {}
        for i in top_idx:
            if scores[i] < min_score:
                continue
            c = self.chunks[i]
            if not include_notes and c.get("kind") == "note":
                continue
            shared = query_terms & set(analyzer(c["text"]))
            if len(shared) < min_shared:
                continue
            where = f"{c['source']} › {c['section']}" if "section" in c else f"{c['source']}, p.{c['page']}"
            if where in seen or from_source.get(c["source"], 0) >= per_source:
                continue           # overlapping window of the same slide/page, or file at its cap
            seen.add(where)
            from_source[c["source"]] = from_source.get(c["source"], 0) + 1
            parts.append(f"({where}): {c['text']}")
            if len(parts) >= k:
                break
        return "\n".join(parts)

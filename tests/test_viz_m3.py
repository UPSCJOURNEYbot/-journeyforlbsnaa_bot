"""Phase 3 milestone 3: large + real-world PDF validation (no network).

Drives the production direct-MCQ-file flow end to end: emit TXT/MD/PDF
fixtures (Format A / Format B / mixed) -> production parser ->
production service payload -> production renderer -> PyMuPDF + pixel
inspection of the real output PDFs.
"""

from __future__ import annotations

import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz

from pdf_service.render import render_testseries_pdf
from pdf_service.viz import engine as viz_engine
from pdf_service.viz.textstyle import BADGES
from quizbot.creator_bot.handlers import testseries_file as tsf
from quizbot.creator_bot.handlers.reports import _build_testseries_payload
from tests.m3_seeds import SHOWCASE
from tests.m3_volume import build_pool, default_track

FONTS_DIR = Path(__file__).resolve().parent.parent / "pdf_service" / "fonts"
DEJAVU = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
LETTERS = "ABCD"

# Minimum visual coverage per size (post-parse decisions; conservative).
FLOORS = {10: (2, 6), 55: (8, 12), 100: (15, 18), 200: (35, 30),
          300: (50, 40)}
TIME_CAPS = {10: 120, 55: 180, 100: 300, 200: 480, 300: 720}
UNICODE_MUST = ["लबासना", "झील", "सांभर"]

POOL = build_pool()


def track_of(entry):
    return entry["track"] or default_track(entry["stem"])


def sol_of(entry):
    """Solution-section token: explicit pin or explanation head."""
    if entry.get("sol"):
        return entry["sol"]
    head = norm(entry["expl"])[:30]
    assert head, "empty explanation needs the No-explanation count check"
    return head


def emit_block(entry, num, style):
    """One fixture block in Format A / B-letter / B-text style."""
    header = ("Q.%d." % num, "Q%d" % num, "Question %d." % num)[num % 3]
    lines = [header + " " + entry["stem"].split("\n")[0]]
    lines.extend(entry["stem"].split("\n")[1:])
    opts = entry["opts"]
    if style == "A":
        low = (num % 5 == 3)
        delim = ")" if num % 2 == 0 else "."
        for j, opt in enumerate(opts):
            letter = LETTERS[j]
            shown = letter.lower() if low else letter
            mark = " ✅" if j == entry["correct"] else ""
            lines.append("%s%s %s%s" % (shown, delim, opt, mark))
        if entry["expl"]:
            parts = entry["expl"].split("\n")
            lines.append("Ex: " + parts[0])
            lines.extend(parts[1:])
    else:
        for j, opt in enumerate(opts):
            lines.append("%s) %s" % (LETTERS[j], opt))
        if style == "Bl":
            spell = ("%s", "%s)", "(%s)", "%s.")[num % 4] % LETTERS[entry["correct"]]
            if num % 4 == 1:
                spell = spell.lower()
            lines.append("Answer: " + spell)
        else:
            lines.append("Answer: " + opts[entry["correct"]])
        if entry["expl"]:
            parts = entry["expl"].split("\n")
            lines.append("Solution: " + parts[0])
            lines.extend(parts[1:])
    return "\n".join(lines)


def emit_text(entries, mode):
    styles = {"A": ["A"], "B": ["Bl", "Bt"], "mixed": ["A", "Bl", "Bt"]}[mode]
    blocks = [emit_block(e, i + 1, styles[i % len(styles)])
              for i, e in enumerate(entries)]
    return "\n\n".join(blocks) + "\n"


def emit_md(entries, mode):
    return ("# M3 Mock Test\n**Read every question carefully.**\n\n"
            + emit_text(entries, mode))


def emit_pdf(entries, mode):
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos
    assert DEJAVU.exists(), "test fixture needs %s" % DEJAVU
    text = emit_text(entries, mode).replace("✅", "✓")
    pdf = FPDF(format="A4")
    pdf.add_font("hind", "", str(FONTS_DIR / "Hind-Regular.ttf"))
    pdf.add_font("dvs", "", str(DEJAVU))
    lines = text.split("\n")
    per_page = 58
    pages = [lines[i:i + per_page] for i in range(0, len(lines), per_page)]
    for page_no, chunk in enumerate(pages, 1):
        pdf.add_page()
        for ln in ["M3 MOCK TEST"] + chunk + ["M3 MOCK TEST FOOT"]:
            pdf.set_font("dvs" if "✓" in ln else "hind", size=11)
            pdf.multi_cell(0, 6, ln if ln else " ",
                           new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    return bytes(pdf.output())


def norm(text):
    return re.sub(r"\s+", " ", text)


class M3Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="viz-m3-")
        cls._n = 0

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _pipeline(self, entries, filename, data, title, cap):
        t0 = time.time()
        r = tsf.process_testseries_upload(data, filename)
        self.assertTrue(r.ok, (r.problems or [r.error])[:3])
        self.assertEqual(r.total_blocks, len(entries))
        self.assertEqual(len(r.questions), len(entries))
        self.assertEqual(r.problems, [])
        self.assertEqual([q.correct_index for q in r.questions],
                         [e["correct"] for e in entries])
        payload = _build_testseries_payload(
            [{"quiz_name": filename,
              "questions": [q.to_payload() for q in r.questions]}],
            "keyonly", title)
        qs = payload["questions_json"]
        self.assertEqual(len(qs), len(entries))
        self.assertEqual(len({q["question"] for q in qs}), len(entries))
        type(self)._n += 1
        out = str(Path(self._tmp.name) / ("m3-%d.pdf" % type(self)._n))
        info = render_testseries_pdf(
            qs, exam_title=title, tagline="Test Series",
            quiz_names=[filename], solution_display="end", output_path=out)
        dt = time.time() - t0
        self.assertLess(dt, cap, "parse+render took %.1fs" % dt)
        decided = [viz_engine.safe_decide_visual(
            q["question"], tuple(q["options"]), q["explanation"]) for q in qs]
        return {"path": out, "info": info, "qs": qs, "decided": decided,
                "seconds": dt, "result": r}

    def _inspect(self, res, entries, check_visual_counts=True):
        qs, decided = res["qs"], res["decided"]
        n = len(entries)
        self.assertEqual(res["info"]["questions"], n)
        with fitz.open(res["path"]) as doc:
            npages = doc.page_count
            self.assertLess(npages, 10 * n + 10)
            raw = "\n".join(p.get_text() for p in doc)
            full = norm(raw)
            self.assertIn("Questions", full)
            self.assertIn("Answer Key", full)
            self.assertIn("Detailed Solutions", full)
            q_sec = full.split("Answer Key")[0]
            key_sec = full.split("Answer Key")[1].split("Detailed Solutions")[0]
            sol_sec = full.split("Detailed Solutions")[1]
            sol_raw = raw.split("Detailed Solutions")[1]
            # Key exact: every number with the parsed correct letter.
            found = dict((int(m.group(1)), m.group(2))
                         for m in re.finditer(r"Q(\d+) – ([A-D])", key_sec))
            want = {i + 1: LETTERS[q.correct_index]
                    for i, q in enumerate(res["result"].questions)}
            self.assertEqual(found, want)
            # Solutions exact order, no loss, no dupes.
            order = [int(m.group(1))
                     for m in re.finditer(r"^Q(\d+)\.", sol_raw, flags=re.M)]
            self.assertEqual(order, list(range(1, n + 1)))
            # Every stem track in questions; every explanation (or the
            # No-explanation note) in detailed solutions.
            for e in entries:
                track = track_of(e)
                self.assertIn(track, q_sec, "lost in questions: %r" % track)
            for e in entries:
                if e["expl"]:
                    token = sol_of(e)
                    self.assertIn(token, sol_sec,
                                  "lost in solutions: %r" % token)
            want_blank = sum(1 for e in entries if not e["expl"])
            self.assertEqual(sol_sec.count("No explanation provided."),
                             want_blank)
            # Visual counts agree decision -> render -> inspect.
            maps = sum(1 for s in decided if s and s.visual_type in
                       ("location_map", "regional_map"))
            diagrams = {}
            for s in decided:
                if s and s.visual_type not in ("location_map", "regional_map"):
                    diagrams[s.visual_type] = diagrams.get(s.visual_type, 0) + 1
            if check_visual_counts:
                self.assertEqual(full.count("Not to scale"), maps)
                # Grouped classifications render the badge word twice
                # (badge + generic root box); flat ones render it once.
                expected = {}
                for s in decided:
                    if s is None or s.visual_type in ("location_map",
                                                      "regional_map"):
                        continue
                    mult = 2 if (s.visual_type == "classification"
                                 and s.payload.get("groups")) else 1
                    vtype = s.visual_type
                    expected[vtype] = expected.get(vtype, 0) + mult
                self.assertEqual(set(expected), set(diagrams))
                for vtype, cnt in expected.items():
                    self.assertEqual(full.count(BADGES[vtype]), cnt, vtype)
            else:
                self.assertNotIn("Not to scale", full)
                for badge in set(BADGES.values()):
                    self.assertNotIn(badge, full)
            # Per-page checks: header/footer/branding/bounds/attachment.
            markers = ["Not to scale"] + [BADGES[t] for t in diagrams]
            bold_answer = bold_qnum = bold_head = False
            for pno in range(npages):
                page = doc[pno]
                text = page.get_text()
                self.assertIn("Journey for लबासना", text, "page %d" % pno)
                self.assertIn("Page %d / %d" % (pno + 1, npages), text)
                self.assertTrue(text.strip(), "blank page %d" % pno)
                w, h = page.rect.width, page.rect.height
                for x0, y0, x1, y1, _t, _b, _ty in page.get_text("blocks"):
                    self.assertGreaterEqual(x0, -1)
                    self.assertGreaterEqual(y0, -1)
                    self.assertLessEqual(x1, w + 1)
                    self.assertLessEqual(y1, h + 1)
                drawings = page.get_drawings()
                for d in drawings:
                    self.assertGreaterEqual(d["rect"].y0, 6)
                    self.assertLessEqual(d["rect"].y1, h - 6)
                    self.assertGreaterEqual(d["rect"].x0, -1)
                    self.assertLessEqual(d["rect"].x1, w + 1)
                present = [m for m in markers if m in norm(text)]
                if present:
                    self.assertGreaterEqual(len(drawings), 5)
                    words = page.get_text("words")
                    flat = [wd[4] for wd in words]
                    d_top = min(d["rect"].y0 for d in drawings)
                    d_bot = max(d["rect"].y1 for d in drawings)
                    for m in present:
                        parts = m.split(" ")
                        hit = False
                        for i in range(len(flat) - len(parts) + 1):
                            if flat[i:i + len(parts)] == parts:
                                ys = [words[i + k][1] for k in range(len(parts))]
                                if min(ys) >= d_top - 3 and min(ys) <= d_bot + 3:
                                    hit = True
                                break
                        self.assertTrue(hit, "%r detached on p%d" % (m, pno))
                for block in page.get_text("dict")["blocks"]:
                    for line in block.get("lines", []):
                        for span in line["spans"]:
                            bold = (span["flags"] & 16) or "bold" in span["font"].lower()
                            if not bold:
                                continue
                            if "Answer:" in span["text"]:
                                bold_answer = True
                            if re.search(r"^Q\d+\.", span["text"]):
                                bold_qnum = True
                            if "Detailed Solutions" in span["text"]:
                                bold_head = True
            self.assertTrue(bold_answer, "no bold Answer: span")
            self.assertTrue(bold_qnum, "no bold Q number span")
            self.assertTrue(bold_head, "no bold section header")
            for token in UNICODE_MUST:
                if any(token in (e["stem"] + e["expl"]) for e in entries):
                    self.assertIn(token, full)
        return {"maps": maps, "diagrams": sum(diagrams.values())}

    def _pixels(self, res, need_map=True, need_diagram=True):
        with fitz.open(res["path"]) as doc:
            seen_map = seen_dia = False
            for page in doc:
                text = norm(page.get_text())
                if "Not to scale" in text:
                    seen_map = True
                    self._inked(page, "map")
                elif any(b in text for b in set(BADGES.values())):
                    seen_dia = True
                    self._inked(page, "diagram")
            if need_map:
                self.assertTrue(seen_map, "no map page rendered")
            if need_diagram:
                self.assertTrue(seen_dia, "no diagram page rendered")

    def _inked(self, page, kind):
        pix = page.get_pixmap(dpi=72)
        raw = pix.samples
        total = pix.width * pix.height
        white = 0
        for i in range(0, len(raw), 3):
            if raw[i] > 245 and raw[i + 1] > 245 and raw[i + 2] > 245:
                white += 1
        frac = 1.0 - white / total
        self.assertGreater(frac, 0.005, "%s page blank" % kind)
        self.assertLess(frac, 0.6, "%s page over-inked" % kind)

    def _showcase_pins(self, res):
        for i in range(10):
            spec = res["decided"][i]
            got = spec.visual_type if spec else None
            self.assertEqual(got, SHOWCASE[i]["expect"],
                             "showcase %d (%s)" % (i, SHOWCASE[i]["tag"]))


class SizeSweepCases(M3Base):
    def _sweep(self, n):
        entries = POOL[:n]
        data = emit_text(entries, "mixed").encode("utf-8")
        res = self._pipeline(entries, "m3-%d.txt" % n, data,
                             "M3 Mock %d" % n, TIME_CAPS[n])
        got = self._inspect(res, entries)
        self._showcase_pins(res)
        floors = FLOORS[n]
        self.assertGreaterEqual(got["maps"], floors[0])
        self.assertGreaterEqual(got["diagrams"], floors[1])
        if n in (10, 55):
            self._pixels(res)
        return res

    def test_size_010_txt_mixed(self):
        self._sweep(10)

    def test_size_055_txt_mixed(self):
        self._sweep(55)

    def test_size_100_txt_mixed(self):
        self._sweep(100)

    def test_size_200_txt_mixed(self):
        self._sweep(200)

    def test_size_300_txt_mixed(self):
        self._sweep(300)


class FormatMatrixCases(M3Base):
    def test_txt_format_a_10(self):
        entries = POOL[:10]
        res = self._pipeline(entries, "m3-a.txt",
                             emit_text(entries, "A").encode("utf-8"),
                             "M3 Format A", TIME_CAPS[10])
        self._inspect(res, entries)
        self._showcase_pins(res)
        self._pixels(res)

    def test_txt_format_b_10(self):
        entries = POOL[:10]
        res = self._pipeline(entries, "m3-b.txt",
                             emit_text(entries, "B").encode("utf-8"),
                             "M3 Format B", TIME_CAPS[10])
        self._inspect(res, entries)
        self._showcase_pins(res)

    def test_md_format_a_10(self):
        entries = POOL[:10]
        res = self._pipeline(entries, "m3-a.md",
                             emit_md(entries, "A").encode("utf-8"),
                             "M3 MD A", TIME_CAPS[10])
        self._inspect(res, entries)
        self._showcase_pins(res)

    def test_md_format_b_10(self):
        entries = POOL[:10]
        res = self._pipeline(entries, "m3-b.md",
                             emit_md(entries, "B").encode("utf-8"),
                             "M3 MD B", TIME_CAPS[10])
        self._inspect(res, entries)
        self._showcase_pins(res)

    def test_md_mixed_10(self):
        entries = POOL[:10]
        res = self._pipeline(entries, "m3-m.md",
                             emit_md(entries, "mixed").encode("utf-8"),
                             "M3 MD Mixed", TIME_CAPS[10])
        self._inspect(res, entries)
        self._showcase_pins(res)


class PdfInputCases(M3Base):
    def test_pdf_format_b_10(self):
        entries = POOL[:10]
        res = self._pipeline(entries, "m3-b.pdf", emit_pdf(entries, "B"),
                             "M3 PDF B", TIME_CAPS[10])
        self.assertTrue(res["result"].is_pdf)
        self.assertFalse(res["result"].ocr_used)
        self._inspect(res, entries)
        self._showcase_pins(res)
        self._pixels(res)

    def test_pdf_mixed_10(self):
        entries = POOL[:10]
        res = self._pipeline(entries, "m3-m.pdf", emit_pdf(entries, "mixed"),
                             "M3 PDF Mixed", TIME_CAPS[10])
        self._inspect(res, entries)
        self._showcase_pins(res)

    def test_pdf_mixed_55(self):
        entries = POOL[:55]
        res = self._pipeline(entries, "m3-m55.pdf", emit_pdf(entries, "mixed"),
                             "M3 PDF 55", TIME_CAPS[55])
        self._inspect(res, entries)
        self._showcase_pins(res)


class RobustnessCases(M3Base):
    def test_statements_preserved_55(self):
        entries = POOL[:55]
        r = tsf.process_testseries_upload(
            emit_text(entries, "mixed").encode("utf-8"), "m3-st.txt")
        self.assertTrue(r.ok, (r.problems or [r.error])[:3])
        stems = [q.question for q in r.questions]
        self.assertTrue(any("1. The Constitution is the supreme law" in s
                            for s in stems))
        self.assertTrue(any("3. El Nino can weaken the monsoon rains." in s
                            for s in stems))
        for q in r.questions:
            self.assertGreaterEqual(len(q.options), 2)
            self.assertLessEqual(len(q.options), 10)

    def test_failure_injection_55(self):
        entries = POOL[:55]
        data = emit_text(entries, "mixed").encode("utf-8")
        with patch("pdf_service.viz.mapdraw.draw_map",
                   side_effect=RuntimeError("map boom")), \
             patch("pdf_service.viz.templates.draw_diagram",
                   side_effect=RuntimeError("diagram boom")):
            res = self._pipeline(entries, "m3-fail.txt", data,
                                 "M3 Fail55", TIME_CAPS[55])
        self.assertEqual(res["info"]["questions"], 55)
        self._inspect(res, entries, check_visual_counts=False)

    def test_missing_explanations_10(self):
        entries = [dict(e, expl="") for e in POOL[:10]]
        res = self._pipeline(entries, "m3-noexpl.txt",
                             emit_text(entries, "mixed").encode("utf-8"),
                             "M3 NoExpl", TIME_CAPS[10])
        self._inspect(res, entries)

    def test_skip_set_renders_clean(self):
        entries = [e for e in POOL
                   if e["tag"] in ("skip/unknown", "skip/world",
                                   "skip/world2", "none/arithmetic2",
                                   "skip/no-expl-none", "none/arithmetic")]
        self.assertEqual(len(entries), 6)
        res = self._pipeline(entries, "m3-skip.txt",
                             emit_text(entries, "mixed").encode("utf-8"),
                             "M3 Skip", TIME_CAPS[10])
        self.assertTrue(all(s is None for s in res["decided"]))
        self._inspect(res, entries)


if __name__ == "__main__":
    unittest.main(verbosity=2)

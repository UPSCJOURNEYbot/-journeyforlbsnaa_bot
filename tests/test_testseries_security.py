"""Part L: test-series security boundaries (adversarial inputs).

Proves with real renders:

  * hostile text (HTML/JS, URLs, format strings, controls, bidi
    overrides, zero-width chars, emoji/CJK Greek outside the font,
    lone surrogates, multi-KB unbreakable tokens) renders as inert
    text: no crash, no U+FFFD, controls/bidi/surrogates stripped,
    geometry intact;
  * setup images: a 117 KB 6000x6000 PNG (which used to cost ~275 MB
    of transient RAM) is now rejected by a header-only 16 MP dimension
    cap before any pixel is decoded; corrupt/oversized/non-string
    inputs stay None; normal images still work;
  * wizard caption/filename neutralise Telegram-markup metacharacters
    and path separators.
"""

from __future__ import annotations

import base64
import io
import re
import tempfile
import time
import unittest
import warnings
from pathlib import Path

warnings.simplefilter("ignore")
import fitz  # noqa: E402  (PyMuPDF; already a project dependency)

from pdf_service.render import (  # noqa: E402
    _setup_image,
    render_testseries_pdf,
    sanitize_text,
)

EVIL = (
    "SECQ <script>alert(1)</script> <b>bold</b> "
    "javascript:alert(1) https://evil.example/x "
    "%s %d {0} {evil} $HOME "
    "line1\nline2\tTabbed "
    "RTL\U0000202EOVERRIDE bidi\U0000202Aembed\U0000202Cpop "
    "iso\U00002066x\U00002069y bom\U0000FEFFmark "
    "zw\U0000200bspace\U0000200cnonjoin\U0000200djoiner "
    "emoji \U0001F600 flag \U0001F1EE\U0001F1F3 cjk \U00004E2D greek \U000003B1 "
    "SUPERLONG" + "A" * 3000 + "END "
    "surrogate\U0000D800X tail "
    "under_score *star* [bracket] (paren)"
)


class AdversarialTextCases(unittest.TestCase):
    def test_evil_renders_inert(self) -> None:
        qs = [{"question": "Q " + EVIL, "options": ["O " + EVIL, "plain"],
               "correct_option_id": 0, "explanation": "E " + EVIL}]
        with tempfile.TemporaryDirectory(prefix="tsx-") as tmp:
            path = Path(tmp) / "evil.pdf"
            info = render_testseries_pdf(
                qs, exam_title="T " + EVIL, tagline="TS", quiz_names=["QZ"],
                solution_display="inline", output_path=path)
            self.assertEqual(info["questions"], 1)
            doc = fitz.open(path)
            try:
                text = "\n".join(p.get_text() for p in doc)
                # Inert: markup/URLs/format strings visible as plain text.
                for probe in ("<script>", "alert(1)", "javascript:",
                              "https://evil.example/x", "%s", "{0}",
                              "under_score", "*star*", "[bracket]",
                              "SUPERLONG", "END", "tail"):
                    self.assertIn(probe, text, probe)
                # Stripped: bidi controls, BOM, lone surrogate.
                for bad in ("\U0000202E", "\U0000202A", "\U0000202C",
                            "\U00002066", "\U00002069", "\U0000FEFF",
                            "\U0000D800", chr(0xFFFD)):
                    self.assertNotIn(bad, text, repr(bad))
                # Surrounding text survives the stripped characters.
                for probe in ("RTLOVERRIDE", "bidiembedpop", "isox",
                              "bommark", "surrogateX"):
                    self.assertIn(probe, text, probe)
                # Geometry intact despite the 3000-char token.
                for pno, page in enumerate(doc):
                    w, h = page.rect.width, page.rect.height
                    for b in [b for b in page.get_text("dict")["blocks"]
                              if b["type"] == 0]:
                        x0, y0, x1, y1 = b["bbox"]
                        self.assertGreaterEqual(x0, -0.5, f"page {pno}")
                        self.assertGreaterEqual(y0, -0.5, f"page {pno}")
                        self.assertLessEqual(x1, w + 0.5, f"page {pno}")
                        self.assertLessEqual(y1, h + 0.5, f"page {pno}")
            finally:
                doc.close()

    def test_sanitize_strips_controls(self) -> None:
        for raw in ("a\x00b", "a\x08b", "a\x07b", "a\x1bb", "a\x7fb"):
            kept, _ = sanitize_text(raw, 50)
            self.assertEqual(kept, "ab", repr(raw))
        kept, dropped = sanitize_text("a\U0000D800b", 50)
        self.assertEqual(kept, "ab")
        self.assertEqual(dropped, 1)


def _solid_png(w: int, h: int) -> str:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 200, 60)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


class SetupImageCases(unittest.TestCase):
    def test_dimension_bomb_rejected(self) -> None:
        t0 = time.time()
        self.assertIsNone(_setup_image(_solid_png(6000, 6000)))
        self.assertLess(time.time() - t0, 10)

    def test_dimension_cap_boundary(self) -> None:
        # 4000x4000 = exactly 16 MP: accepted; one row more: rejected.
        got = _setup_image(_solid_png(4000, 4000))
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual((got[1], got[2]), (800, 800))
        self.assertIsNone(_setup_image(_solid_png(4001, 4000)))

    def test_bad_inputs_stay_none(self) -> None:
        self.assertIsNone(_setup_image(None))
        self.assertIsNone(_setup_image(""))
        self.assertIsNone(_setup_image(12345))
        self.assertIsNone(_setup_image("!!!not-base64!!!"))
        self.assertIsNone(_setup_image(
            base64.b64encode(b"not an image at all" * 100).decode()))

    def test_normal_image_works(self) -> None:
        got = _setup_image(_solid_png(120, 60))
        self.assertIsNotNone(got)
        assert got is not None
        data, w, h = got
        self.assertTrue(data[:4] == b"\x89PNG")
        self.assertEqual((w, h), (120, 60))


class WizardEscapeCases(unittest.TestCase):
    def test_caption_and_filename_escape(self) -> None:
        from quizbot.creator_bot.handlers.testseries_create import (
            TestSeriesConfig, build_create_caption, build_create_filename)
        cfg = TestSeriesConfig(
            title="A_B*C[D]E`F`G/H\\I:J", subject="S_T*U",
            institute_name="I", total_questions=5, max_marks=10.0,
            marks_correct=2.0, marks_negative=-0.66)
        caption = build_create_caption(cfg, "qids")
        # Legacy-Markdown escaping: [, _, *, `, \ escaped (a bare ] or
        # paren cannot open markup once [ is escaped).
        self.assertIn("A\\_B\\*C\\[D]E\\`F\\`G/H\\\\I:J", caption)
        self.assertNotIn("A_B*C", caption)
        name = build_create_filename(cfg)
        self.assertTrue(name.endswith("Q.pdf"), name)
        self.assertIsNone(re.search(r"[^\w\-.]+", name), name)
        self.assertNotIn("/", name)
        self.assertNotIn("\\", name)


if __name__ == "__main__":
    unittest.main()

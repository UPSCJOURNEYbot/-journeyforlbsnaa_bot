"""Regression tests for the /create parser hotfix.

Each test class pins one defect that was reproduced on the parser as it
stood before the fix:

1. `StandaloneAnswerParagraphTests` — an ``Answer:`` paragraph separated
   from its options by a blank line was never rejoined to its question, so
   the question was reported as ``missing_answer`` and the orphan paragraph
   as ``insufficient_options``. This lost a *variable* number of questions
   depending only on how many answers were written that way, never on the
   question count.
2. `AnswerParagraphSafetyTests` — the rejoin must not rescue a one-option
   question, must not override an answer the question already carries, and
   must not attach arbitrary prose that merely contains the word "Answer".
3. `QualifiedSolutionsHeaderTests` — a "Detailed Solutions" section header
   did not switch the key region to solutions-only, so its ``Qn.  Answer:``
   rows were re-read as answer-key rows and rejected every question whose
   real answer was not the first option.
4. `RunningPageFurnitureTests` — a running PDF header/footer landing inside
   a question split its option list across blocks.
5. `TableFragmentTests` — blank-line padding around a markdown table cut a
   question away from its own table and options, and table row numbers
   could be counted as option labels.
6. `OutOfRangeLabelTests` — a wrapped line beginning with a parenthesised
   Assertion/Reason marker, "(R) ...", was read as an out-of-range option
   label and rejected the whole Assertion-Reason question.
"""

from __future__ import annotations

import io
import os
import unittest

import fitz  # PyMuPDF

from quizbot.creator_bot import parsing


def parse_doc(text: str):
    return parsing.parse_question_document(text)


def questions(text: str) -> int:
    return len(parse_doc(text).questions)


def _question(n: int, *, label_sep: str = ")") -> str:
    return (
        f"Q{n}. Question {n}?\n"
        f"A{label_sep} one\nB{label_sep} two\n"
        f"C{label_sep} three\nD{label_sep} four"
    )


def _doc(n: int, *, answer_paragraph: bool) -> str:
    """`n` questions; answers inline-adjacent or in their own paragraph."""
    parts = []
    for i in range(1, n + 1):
        body = _question(i)
        body += "\n\nAnswer: B" if answer_paragraph else "\nAnswer: B"
        parts.append(body)
    return "\n\n".join(parts)


class StandaloneAnswerParagraphTests(unittest.TestCase):
    """Defect 1: the answer paragraph was orphaned from its question."""

    def test_answer_paragraph_is_rejoined(self):
        doc = parse_doc("Q1. Stem?\nA) one\nB) two\nC) three\nD) four\n\nAnswer: B\n")
        self.assertEqual(len(doc.questions), 1, [s.reason for s in doc.skipped])
        self.assertEqual(doc.questions[0]["correct_option_id"], 1)
        self.assertEqual(doc.skipped, [])

    def test_answer_and_solution_paragraphs_are_rejoined(self):
        doc = parse_doc(
            "Q1. Stem?\nA) one\nB) two\nC) three\nD) four\n\n"
            "Answer: C\n\nSolution: because three.\n")
        self.assertEqual(len(doc.questions), 1, [s.reason for s in doc.skipped])
        self.assertEqual(doc.questions[0]["correct_option_id"], 2)
        self.assertIn("three", doc.questions[0]["explanation"])

    def test_correct_answer_label_variant(self):
        doc = parse_doc(
            "Q1. Stem?\nA) one\nB) two\nC) three\nD) four\n\nCorrect answer: D\n")
        self.assertEqual(len(doc.questions), 1, [s.reason for s in doc.skipped])
        self.assertEqual(doc.questions[0]["correct_option_id"], 3)

    def test_markdown_bold_answer_paragraph(self):
        doc = parse_doc(
            "**Q1.** Stem?\nA) one\nB) two\nC) three\nD) four\n\n"
            "**Answer:** B\n\n**Solution:** reasoning.\n")
        self.assertEqual(len(doc.questions), 1, [s.reason for s in doc.skipped])
        self.assertEqual(doc.questions[0]["correct_option_id"], 1)

    def test_hindi_answer_label(self):
        doc = parse_doc(
            "Q1. प्रश्न?\nक) एक\nख) दो\nग) तीन\nघ) चार\n\nउत्तर: ख\n")
        self.assertEqual(len(doc.questions), 1, [s.reason for s in doc.skipped])
        self.assertEqual(doc.questions[0]["correct_option_id"], 1)

    def test_multiline_options_with_answer_paragraph(self):
        doc = parse_doc(
            "Q1. Stem?\nA) one line one\n   line two\nB) two ✅\nC) three\n"
            "D) four\n\nAnswer: B\n")
        self.assertEqual(len(doc.questions), 1, [s.reason for s in doc.skipped])
        self.assertEqual(len(doc.questions[0]["options"]), 4)

    def test_each_question_keeps_its_own_answer_paragraph(self):
        doc = parse_doc(
            "Q1. First?\nA) one\nB) two\nC) three\nD) four\n\nAnswer: C\n\n"
            "Q2. Second?\nA) one\nB) two\nC) three\nD) four\n\nAnswer: A\n")
        self.assertEqual(len(doc.questions), 2, [s.reason for s in doc.skipped])
        self.assertEqual([q["correct_option_id"] for q in doc.questions], [2, 0])

    def test_count_agnostic_all_answers_in_paragraphs(self):
        for n in (10, 20, 35, 50, 100):
            with self.subTest(questions=n):
                doc = parse_doc(_doc(n, answer_paragraph=True))
                self.assertEqual(len(doc.questions), n,
                                 [s.reason for s in doc.skipped])
                self.assertTrue(all(q["correct_option_id"] == 1
                                    for q in doc.questions))

    def test_count_agnostic_mixed_marker_styles(self):
        """Half inline checkmarks, half answer paragraphs: everything is kept."""
        for n in (10, 20, 35, 50, 100):
            half = n // 2
            parts = []
            for i in range(1, n + 1):
                body = _question(i).replace("B) two", "B) two ✅") \
                    if i <= half else _question(i)
                body += "\n" if i <= half else "\n\nAnswer: B"
                parts.append(body)
            with self.subTest(questions=n, inline=half):
                doc = parse_doc("\n\n".join(parts))
                self.assertEqual(len(doc.questions), n,
                                 [s.reason for s in doc.skipped])

    def test_no_count_based_logic(self):
        """The fix must not depend on the number of questions: the same input
        shape loses nothing at every size, and a single-question document
        behaves exactly like the same question inside a hundred."""
        one = parse_doc("Q1. Stem?\nA) one\nB) two\nC) three\nD) four\n\nAnswer: B\n")
        self.assertEqual(len(one.questions), 1)
        many = parse_doc(_doc(100, answer_paragraph=True))
        self.assertEqual(len(many.questions), 100)


class AnswerParagraphSafetyTests(unittest.TestCase):
    """The rejoin must stay structural, never a guess."""

    def test_one_option_question_is_not_rescued(self):
        doc = parse_doc("Q1. One?\nA) only\n\nAnswer: A\n")
        self.assertEqual(len(doc.questions), 0)
        self.assertEqual(doc.skipped[0].reason, parsing.RE_INSUFFICIENT_OPTIONS)

    def test_one_option_question_stays_rejected_before_a_valid_one(self):
        doc = parse_doc(
            "Q1. One?\nA) only\n\nAnswer: A\n\n"
            "Q2. Two?\nA) a\nB) b\nAnswer: B\n")
        self.assertEqual(len(doc.questions), 1)
        self.assertEqual(doc.questions[0]["question"].strip(), "Two?")
        self.assertEqual({s.reason for s in doc.skipped},
                         {parsing.RE_INSUFFICIENT_OPTIONS})

    def test_existing_answer_is_never_overridden(self):
        doc = parse_doc(
            "Q1. Stem?\nA) one\nB) two\nC) three\nD) four\nAnswer: B\n\n"
            "Answer: D\n")
        self.assertEqual(len(doc.questions), 1)
        self.assertEqual(doc.questions[0]["correct_option_id"], 1)

    def test_prose_mentioning_answer_is_not_attached(self):
        doc = parse_doc(
            "Q1. Stem?\nA) one\nB) two\nC) three\nD) four\n\n"
            "The answer to this question is discussed in the appendix.\n")
        self.assertEqual(len(doc.questions), 0)

    def test_unresolvable_answer_paragraph_is_not_attached(self):
        doc = parse_doc(
            "Q1. Stem?\nA) one\nB) two\nC) three\nD) four\n\n"
            "Answer: as discussed in the appendix.\n")
        self.assertEqual(len(doc.questions), 0)
        self.assertEqual({s.reason for s in doc.skipped},
                         {parsing.RE_MISSING_ANSWER,
                          parsing.RE_INSUFFICIENT_OPTIONS})

    def test_standalone_answer_without_question_is_skipped(self):
        doc = parse_doc("Answer: B\n")
        self.assertEqual(len(doc.questions), 0)
        self.assertEqual(doc.skipped[0].reason, parsing.RE_INSUFFICIENT_OPTIONS)


class QualifiedSolutionsHeaderTests(unittest.TestCase):
    """Defect 3: solution rows re-read as answer keys."""

    @staticmethod
    def _document(header: str) -> str:
        return (
            "Q1. First question?\nA) one\nB) two\nC) three\nD) four\n\n"
            "Q2. Second question?\nA) one\nB) two\nC) three\nD) four\n\n"
            "Answer Key\nQ1 – B\nQ2 – C\n\n"
            f"{header}\n"
            "Q1.  Answer: B\nSolution: first reasoning.\n"
            "Q2.  Answer: C\nSolution: second reasoning.\n")

    def test_detailed_solutions_header_is_recognised(self):
        doc = parse_doc(self._document("Detailed Solutions"))
        self.assertEqual(len(doc.questions), 2, [s.reason for s in doc.skipped])
        self.assertEqual([q["correct_option_id"] for q in doc.questions], [1, 2])

    def test_other_qualified_headers(self):
        for header in ("Complete Solutions", "Full Explanations",
                       "Model Solutions", "Solutions", "SOLUTIONS"):
            with self.subTest(header=header):
                doc = parse_doc(self._document(header))
                self.assertEqual(len(doc.questions), 2,
                                 [s.reason for s in doc.skipped])

    def test_solution_text_carries_no_answer_marker_row(self):
        doc = parse_doc(self._document("Detailed Solutions"))
        self.assertIn("first reasoning", doc.questions[0]["explanation"])
        self.assertNotIn("Answer: B", doc.questions[0]["explanation"])

    def test_unqualified_prose_is_not_a_section_header(self):
        """Only a header line with no payload switches the region."""
        doc = parse_doc(
            "Q1. Only?\nA) one\nB) two\nC) three\nD) four\n\n"
            "Answer Key\nQ1 – D\n\n"
            "Solutions are discussed in the appendix below.\n")
        self.assertEqual(len(doc.questions), 1, [s.reason for s in doc.skipped])
        self.assertEqual(doc.questions[0]["correct_option_id"], 3)


class RunningPageFurnitureTests(unittest.TestCase):
    """Defect 4: page headers/footers splitting a question."""

    FOOTER = "Journey for लबासना  •  Page {} / 12"
    HEADER = "Journey for लबासना  •  UPSC mock 01"

    def test_page_numbered_footer_does_not_split_options(self):
        lines = []
        for i in range(1, 4):
            lines += [f"Q{i}. Question {i}?", "A) one", "B) two",
                      self.FOOTER.format(i), "", self.HEADER,
                      "C) three", "D) four", f"Answer: B", ""]
        doc = parse_doc("\n".join(lines))
        self.assertEqual(len(doc.questions), 3, [s.reason for s in doc.skipped])
        self.assertTrue(all(len(q["options"]) == 4 for q in doc.questions))

    def test_exact_repeat_header_is_dropped(self):
        text = "\n\n".join(
            f"{self.HEADER}\nQ{i}. Question {i}?\nA) one\nB) two\nC) three\n"
            f"D) four\nAnswer: B" for i in range(1, 5))
        doc = parse_doc(text)
        self.assertEqual(len(doc.questions), 4, [s.reason for s in doc.skipped])

    def test_repeated_option_and_question_lines_are_never_furniture(self):
        """Repeats are common in real papers; only non-content lines qualify."""
        text = "\n\n".join(
            "Which of the statements given above are correct?\n"
            "A) one\nB) two\nC) three\nD) four\nAnswer: B"
            for _ in range(6))
        doc = parse_doc(text)
        self.assertEqual(len(doc.questions), 6, [s.reason for s in doc.skipped])
        self.assertTrue(all(len(q["options"]) == 4 for q in doc.questions))

    def test_furniture_removal_never_loses_questions(self):
        """A repeated line that is real content keeps its questions."""
        text = "\n\n".join(
            f"{self.HEADER}\nQ{i}. Question {i}?\nA) one\nB) two\nC) three\n"
            f"D) four\nAnswer: B" for i in range(1, 9))
        self.assertEqual(questions(text), 8)


class TableFragmentTests(unittest.TestCase):
    """Defect 5: table padding cutting a question apart."""

    TABLE_Q = (
        "Q1. Consider the following pairs:\n"
        "| Landform | Feature |\n|---|---|\n"
        "| 1. Seamount | flat top |\n"
        "| 2. Canyon | turbidity |\n"
        "How many of the pairs are correctly matched?\n"
        "A) one\nB) two\nC) three\nD) four\nAnswer: B")

    def test_table_inside_question_keeps_its_options(self):
        doc = parse_doc(self.TABLE_Q)
        self.assertEqual(len(doc.questions), 1, [s.reason for s in doc.skipped])
        self.assertEqual(len(doc.questions[0]["options"]), 4)
        self.assertEqual(doc.questions[0]["correct_option_id"], 1)

    def test_table_question_keeps_table_in_its_stem(self):
        doc = parse_doc(self.TABLE_Q)
        self.assertIn("Seamount", doc.questions[0]["question"])

    def test_numbered_table_rows_are_not_option_labels(self):
        """A table row numbered 11 must not read as an 11th option."""
        text = self.TABLE_Q.replace(
            "| 1. Seamount | flat top |",
            "| 11) Seamount | flat top |").replace(
            "| 2. Canyon | turbidity |", "| 12) Canyon | turbidity |")
        doc = parse_doc(text)
        self.assertEqual(len(doc.questions), 1, [s.reason for s in doc.skipped])
        self.assertEqual(len(doc.questions[0]["options"]), 4)

    def test_table_questions_at_every_size(self):
        for n in (10, 20, 35, 50, 100):
            parts = []
            for i in range(1, n + 1):
                if i % 3 == 0:
                    parts.append(self.TABLE_Q.replace("Q1.",
                                                      f"Q{i}.", 1))
                else:
                    parts.append(_question(i) + "\nAnswer: B")
            with self.subTest(questions=n):
                doc = parse_doc("\n\n".join(parts))
                self.assertEqual(len(doc.questions), n,
                                 [s.reason for s in doc.skipped])

    def test_pdf_text_layer_page_break_inside_table_question(self):
        """End-to-end through the PDF import path with a page-break footer."""
        lines = ["Q1. Consider the pairs:", "| A | B |", "|---|---|",
                 "| 1 | x |", "| 2 | y |", "Journey for LBSNAA  •  Page 1 / 3",
                 "", "Journey for LBSNAA  •  UPSC mock 01",
                 "How many are correct?", "A) one", "B) two",
                 "Journey for LBSNAA  •  Page 2 / 3", "", "C) three",
                 "D) four", "Answer: B", ""]
        doc = fitz.open()
        page = doc.new_page()
        y = 60
        for line in lines:
            page.insert_text((50, y), line, fontsize=10, fontname="helv")
            y += 16
        bio = io.BytesIO()
        doc.save(bio)
        doc.close()
        text = "\n\n".join(p.get_text("text")
                           for p in fitz.open(stream=bio.getvalue(),
                                              filetype="pdf"))
        result = parse_doc(text)
        self.assertEqual(len(result.questions), 1,
                         [s.reason for s in result.skipped])
        self.assertEqual(len(result.questions[0]["options"]), 4)


class OutOfRangeLabelTests(unittest.TestCase):
    """Defect 6: an Assertion/Reason marker read as an 11th option."""

    AR = ("Q1. Consider the following:\n"
          "(A): warm water dissolves gases less readily.\n"
          "(R) पर विचार कीजिए:\n"
          "A) (A) and (R) are true, and (R) explains (A).\n"
          "B) (A) and (R) are true, but (R) does not explain (A).\n"
          "C) (A) is true but (R) is false.\n"
          "D) (A) and (R) are both false.\n"
          "Answer: A")

    def test_assertion_reason_marker_is_not_an_overflow_label(self):
        doc = parse_doc(self.AR)
        self.assertEqual(len(doc.questions), 1, [s.reason for s in doc.skipped])
        self.assertEqual(len(doc.questions[0]["options"]), 4)
        self.assertEqual(doc.questions[0]["correct_option_id"], 0)

    def test_assertion_reason_in_pdf_text_layer(self):
        doc = parse_doc("\n".join(self.AR.splitlines()))
        self.assertEqual(len(doc.questions), 1, [s.reason for s in doc.skipped])

    def test_eleven_contiguous_options_still_rejected(self):
        body = "Q. Eleven?\n" + "\n".join(
            f"{c}) opt {c}" for c in "ABCDEFGHIJK") + "\nAnswer: A"
        result = parsing.parse_question_block_strict(body)
        self.assertEqual(result.reason, parsing.RE_TOO_MANY_OPTIONS)

    def test_contiguous_devanagari_overflow_still_rejected(self):
        body = ("Q. दस?\n"
                + "\n".join(f"{l}) विकल्प" for l in "कखगघङचछजझञ")
                + "\nट) ग्यारहवाँ\nAnswer: क")
        result = parsing.parse_question_block_strict(body)
        self.assertEqual(result.reason, parsing.RE_TOO_MANY_OPTIONS)

    def test_isolated_overflow_label_does_not_reject_a_valid_question(self):
        """A stray "K)" is content, not evidence of eleven options."""
        doc = parse_doc(
            "Q1. Stem?\nA) one\nB) two\nC) three\nD) four\n"
            "K) a stray marker\nAnswer: B")
        self.assertEqual(len(doc.questions), 1, [s.reason for s in doc.skipped])
        self.assertEqual(len(doc.questions[0]["options"]), 4)


class RealFixtureTests(unittest.TestCase):
    """The shipped 35-question PDF, when present in the checkout."""

    FIXTURE = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "TestSeries_UPSC_mock_01_35Q (1).pdf")

    @unittest.skipUnless(os.path.exists(FIXTURE), "35Q fixture not present")
    def test_all_35_questions_recovered_with_correct_answers(self):
        with open(self.FIXTURE, "rb") as fh:
            data = fh.read()
        pdf = fitz.open(stream=data, filetype="pdf")
        text = "\n\n".join(p.get_text("text") for p in pdf)
        pdf.close()
        doc = parse_doc(text)

        expected = {"1": 0, "2": 0, "3": 1, "4": 0, "5": 1, "6": 0, "7": 2,
                    "8": 1, "9": 0, "10": 1, "11": 2, "12": 3, "13": 1,
                    "14": 1, "15": 2, "16": 1, "17": 2, "18": 0, "19": 3,
                    "20": 1, "21": 1, "22": 0, "23": 1, "24": 1, "25": 0,
                    "26": 1, "27": 0, "28": 0, "29": 2, "30": 1, "31": 1,
                    "32": 0, "33": 3, "34": 1, "35": 0}
        self.assertEqual(len(doc.questions), 35,
                         [s.reason for s in doc.skipped])
        # Every question is complete and carries the answer the paper's key
        # prints, in document order: this is a reconstruction, not a guess.
        answers = [q["correct_option_id"] for q in doc.questions]
        self.assertEqual(answers, [expected[str(i)] for i in range(1, 36)])
        self.assertTrue(all(len(q["options"]) == 4 for q in doc.questions))


if __name__ == "__main__":
    unittest.main()

"""Focused regressions for the Part B/C test-series parser fixes.

Canonical parser (quizbot/creator_bot/parsing.py key section):

  * duplicate key rows with the SAME answer are accepted; with DIFFERENT
    answers the question is rejected as conflicting (never guessed);
  * an unreadable key row ("2. Z") rejects only its own question as
    malformed (never leaks into a neighbour's solution);
  * an EXPLANATIONS / समाधान region inside the key section carries
    per-ordinal solutions, never key rows;
  * an out-of-range key letter rejects its question; an unknown ordinal
    (99) warns instead;
  * option 10 ("1. 10", "Q1-10") maps to index 9; ordinal 10 parses as
    ten (the 10|[1-9] alternation), digit answers stay 1-based.

Direct-file parser (testseries_file.py):

  * bare "1." / "1)" / Devanagari "१." headers start questions, sharing
    one sequential counter with Q-shaped headers (mid-list chunks keep
    their own numbers);
  * non-sequential bare headers, digit-led rests ("2.5"), years, and
    "e.g./i.e." lines never split blocks;
  * statement-only input is honestly reported (no false questions);
  * Devanagari options (क-ञ, "क)") and "व्याख्या:" explanations parse.
"""

from __future__ import annotations

import unittest

from quizbot.creator_bot import parsing as P
from quizbot.creator_bot.handlers import testseries_file as tsf

Q2 = "1. First?\nA) a\nB) b\n2. Second?\nA) a\nB) b\n"


class CanonicalKeyCases(unittest.TestCase):
    def test_duplicate_same_answer_accepted(self) -> None:
        r = P.parse_question_document(Q2 + "\nANSWER KEY\n1-A\n1-A\n2-B\n")
        self.assertEqual([q["correct_option_id"] for q in r.questions],
                         [0, 1])
        self.assertEqual(r.skipped, [])

    def test_duplicate_different_answer_conflicts(self) -> None:
        r = P.parse_question_document(Q2 + "\nANSWER KEY\n1-A\n1-B\n2-B\n")
        self.assertEqual(len(r.questions), 1)
        self.assertEqual(r.questions[0]["correct_option_id"], 1)
        self.assertEqual(len(r.skipped), 1)
        self.assertEqual(r.skipped[0].reason, P.RE_CONFLICTING_ANSWERS)
        self.assertEqual(r.skipped[0].ordinal, 1)

    def test_malformed_row_rejects_only_its_question(self) -> None:
        r = P.parse_question_document(Q2 + "\nANSWER KEY\n1-A\n2. Z\n")
        self.assertEqual(len(r.questions), 1)
        self.assertEqual(r.questions[0]["correct_option_id"], 0)
        self.assertEqual(r.questions[0].get("explanation") or "", "")
        self.assertEqual(len(r.skipped), 1)
        self.assertEqual(r.skipped[0].reason, P.RE_MALFORMED_ANSWER_KEY)
        self.assertEqual(r.skipped[0].ordinal, 2)

    def test_explanations_region_carries_solutions(self) -> None:
        r = P.parse_question_document(
            Q2 + "\nANSWER KEY\n1-A\n2-B\nEXPLANATIONS\n"
            "1. Alpha first because reasons.\n"
            "2. Beta second despite doubts.\n")
        self.assertEqual([q["correct_option_id"] for q in r.questions],
                         [0, 1])
        self.assertIn("Alpha first", r.questions[0]["explanation"])
        self.assertIn("Beta second", r.questions[1]["explanation"])
        self.assertEqual(r.skipped, [])

    def test_samadhan_region_carries_solutions(self) -> None:
        r = P.parse_question_document(
            Q2 + "\nANSWER KEY\n1-A\n2-B\nसमाधान\n1. पहला हल यहाँ है।\n")
        self.assertEqual([q["correct_option_id"] for q in r.questions],
                         [0, 1])
        self.assertIn("पहला हल", r.questions[0]["explanation"])

    def test_out_of_range_letter_rejects(self) -> None:
        r = P.parse_question_document(
            "1. First?\nA) a\nB) b\n\nANSWER KEY\n1-D\n")
        self.assertEqual(r.questions, [])
        self.assertEqual(len(r.skipped), 1)
        self.assertEqual(r.skipped[0].reason, P.RE_ANSWER_OUT_OF_RANGE)

    def test_unknown_ordinal_warns(self) -> None:
        r = P.parse_question_document(Q2 + "\nANSWER KEY\n1-A\n2-B\n99-A\n")
        self.assertEqual(len(r.questions), 2)
        self.assertTrue(any("99" in w for w in r.warnings), r.warnings)

    def test_option_ten_dot_form(self) -> None:
        q10 = ("1. Ten?\n"
               + "".join(f"{chr(65 + k)}) o{k}\n" for k in range(10)))
        r = P.parse_question_document(q10 + "\nANSWER KEY\n1. 10\n")
        self.assertEqual(len(r.questions), 1)
        self.assertEqual(r.questions[0]["correct_option_id"], 9)

    def test_option_ten_hyphen_form(self) -> None:
        q10 = ("1. Ten?\n"
               + "".join(f"{chr(65 + k)}) o{k}\n" for k in range(10)))
        r = P.parse_question_document(q10 + "\nANSWER KEY\nQ1-10\n")
        self.assertEqual(len(r.questions), 1)
        self.assertEqual(r.questions[0]["correct_option_id"], 9)

    def test_ordinal_ten_and_digit_answers(self) -> None:
        doc = "".join(f"{i}. Q{i}?\nA) a\nB) b\n" for i in range(1, 11))
        key = "".join(f"{i}-{'A' if i % 2 else 'B'}\n" for i in range(1, 11))
        r = P.parse_question_document(doc + "\nANSWER KEY\n" + key)
        self.assertEqual(len(r.questions), 10)
        self.assertEqual(r.questions[9]["correct_option_id"], 1)
        r2 = P.parse_question_document(Q2 + "\nANSWER KEY\n1-1\n2-2\n")
        self.assertEqual([q["correct_option_id"] for q in r2.questions],
                         [0, 1])


class FileBareHeaderCases(unittest.TestCase):
    def test_bare_dot_and_paren(self) -> None:
        r = tsf.parse_testseries_text(
            "1. What is 1?\nA) a ✅\nB) b\nC) c\nD) d\n\n"
            "2) What is 2?\nA) a\nB) b ✅\nC) c\nD) d\n")
        self.assertTrue(r.ok, r.error or r.problems)
        self.assertEqual([(q.number, q.correct_index) for q in r.questions],
                         [(1, 0), (2, 1)])

    def test_devanagari_numeral_header(self) -> None:
        r = tsf.parse_testseries_text(
            "१. What is 1?\nA) a ✅\nB) b\nC) c\nD) d\n")
        self.assertTrue(r.ok, r.error or r.problems)
        self.assertEqual(r.questions[0].number, 1)

    def test_shared_counter_across_shapes(self) -> None:
        r = tsf.parse_testseries_text(
            "Q1. First?\nA) a ✅\nB) b\n\n"
            "2. Second?\nA) a\nB) b ✅\n\n"
            "Q3. Third?\nA) a ✅\nB) b\n")
        self.assertTrue(r.ok, r.error or r.problems)
        self.assertEqual([q.number for q in r.questions], [1, 2, 3])

    def test_mid_list_chunk_keeps_numbers(self) -> None:
        r = tsf.parse_testseries_text(
            "5. Fifth?\nA) a ✅\nB) b\n\n6. Sixth?\nA) a\nB) b ✅\n")
        self.assertTrue(r.ok, r.error or r.problems)
        self.assertEqual([(q.number, q.correct_index) for q in r.questions],
                         [(5, 0), (6, 1)])

    def test_nonsequential_bare_rejected_honestly(self) -> None:
        r = tsf.parse_testseries_text(
            "1. First?\nA) a ✅\nB) b\n\n5. Fifth?\nA) a ✅\nB) b\n")
        self.assertFalse(r.ok)
        self.assertEqual(r.questions, [])
        self.assertEqual(len(r.problems), 2)

    def test_digit_rest_and_years_do_not_split(self) -> None:
        r = tsf.parse_testseries_text(
            "1. What is 2.5 percent of 100?\nA) 2.5 ✅\nB) 25\n")
        self.assertTrue(r.ok, r.error or r.problems)
        self.assertEqual(len(r.questions), 1)
        r2 = tsf.parse_testseries_text(
            "Q1. In 1947 India won freedom. Discuss 1857 revolt.\n"
            "A) a ✅\nB) b\n")
        self.assertTrue(r2.ok, r2.error or r2.problems)
        self.assertEqual(len(r2.questions), 1)

    def test_statements_only_not_questions(self) -> None:
        r = tsf.parse_testseries_text(
            "Consider the following statements:\n"
            "1. The sky is blue.\n2. Grass is green.\n3. Snow is white.\n")
        self.assertFalse(r.ok)
        self.assertEqual(r.questions, [])
        self.assertIn("No questions detected", r.error)

    def test_abbreviations_do_not_split(self) -> None:
        r = tsf.parse_testseries_text(
            "Q1. Explain, e.g. with examples, this:\n"
            "some stem e.g. like this\ni.e. truly so\n"
            "A) a ✅\nB) b\n")
        self.assertTrue(r.ok, r.error or r.problems)
        self.assertEqual(len(r.questions), 1)

    def test_devanagari_options_and_explanation(self) -> None:
        r = tsf.parse_testseries_text(
            "Q1. राजधानी क्या है?\n"
            "क) दिल्ली ✅\nख) मुंबई\nग) चेन्नई\nघ) कोलकाता\n"
            "व्याख्या: दिल्ली भारत की राजधानी है।\n")
        self.assertTrue(r.ok, r.error or r.problems)
        self.assertEqual(r.questions[0].correct_index, 0)
        self.assertEqual(len(r.questions[0].options), 4)
        self.assertIn("राजधानी", r.questions[0].explanation)


if __name__ == "__main__":
    unittest.main()

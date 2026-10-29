"""Evaluation regressions: punctuation must not alter abstention correctness."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'eval/longterm'))
from run_longterm import grade


class ScoringTests(unittest.TestCase):
    def test_equivalent_apostrophes_are_all_abstentions(self):
        q = {'q': 'An unanswerable question', 'answer': None, 'category': 'abstention'}
        for answer in ("I don't remember.", 'I don’t remember.', 'I donʼt remember.',
                       'I DONT KNOW.', '我不记得了', 'No information is available.'):
            with self.subTest(answer=answer):
                self.assertTrue(grade(None, q, answer))

    def test_asserted_answer_is_not_an_abstention(self):
        q = {'q': 'An unanswerable question', 'answer': None, 'category': 'abstention'}
        for answer in ('A trophy.', 'They saw a meteor shower.', 'I don’t think it was Tuesday.'):
            with self.subTest(answer=answer):
                self.assertFalse(grade(None, q, answer))

    def test_abstention_on_answerable_question_remains_wrong(self):
        q = {'q': 'Which subject?', 'answer': 'astronomy', 'category': 'extraction'}
        self.assertFalse(grade(None, q, 'I don’t remember.'))


if __name__ == '__main__':
    unittest.main(verbosity=2)

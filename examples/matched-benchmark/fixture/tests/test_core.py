import unittest
from textkit.core import normalize_whitespace

class CoreTests(unittest.TestCase):
    def test_whitespace(self):
        self.assertEqual(normalize_whitespace(" a  b "), "a b")

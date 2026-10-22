import unittest
from maths import clamp

class ClampTests(unittest.TestCase):
    def test_above(self):
        self.assertEqual(clamp(9, 0, 5), 5)

    def test_below(self):
        self.assertEqual(clamp(-2, 0, 5), 0)

    def test_inside(self):
        self.assertEqual(clamp(3, 0, 5), 3)

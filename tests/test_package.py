"""Package entry point tests."""

from __future__ import annotations

import unittest

import load_balancer
from load_balancer import __version__


class PackageTests(unittest.TestCase):
    def test_version(self):
        self.assertEqual(__version__, "0.1.0")
        self.assertEqual(load_balancer.__version__, "0.1.0")


if __name__ == "__main__":
    unittest.main()

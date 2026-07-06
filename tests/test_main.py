"""Smoke tests for package entry points."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import unittest
from unittest.mock import patch

from load_balancer.__main__ import main


class MainEntryTests(unittest.TestCase):
    def test_main_help_exits_zero(self):
        out = io.StringIO()
        with patch("sys.argv", ["load-balancer", "--help"]):
            with redirect_stdout(out):
                with self.assertRaises(SystemExit) as exc:
                    main()
            self.assertEqual(exc.exception.code, 0)


if __name__ == "__main__":
    unittest.main()

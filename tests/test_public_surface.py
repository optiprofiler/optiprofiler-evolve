from __future__ import annotations

import unittest

import optiprofiler_evolve


class PublicSurfaceTests(unittest.TestCase):
    def test_only_evolve_and_version_are_exported(self) -> None:
        self.assertEqual(optiprofiler_evolve.__all__, ["__version__", "evolve"])


if __name__ == "__main__":
    unittest.main()

"""Регрессия: внутренние пути Windows не должны нарушать политику входных путей."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bamboo.core import init, new_job, safe, snapshot


class PortablePathsTests(unittest.TestCase):
    def test_snapshot_passes_portable_strings_to_path_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            init(root)
            new_job(root, "portable-path", "Проверка путей", ["card"], [])
            seen = []

            def checked_safe(base, relative):
                self.assertIsInstance(relative, str)
                self.assertNotIn("\\", relative)
                seen.append(relative)
                return safe(base, relative)

            with patch("bamboo.core.safe", side_effect=checked_safe):
                result = snapshot(root, "portable-path")
            self.assertEqual(len(result), 64)
            self.assertIn("content/voice/README.md", seen)


if __name__ == "__main__":
    unittest.main()

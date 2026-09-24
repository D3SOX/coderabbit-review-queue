import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from PySide6.QtGui import QImage


ROOT = Path(__file__).resolve().parents[1]


class InstalledIconTests(unittest.TestCase):
    def test_taskbar_icons_have_transparent_corners(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / 'data'
            bin_dir = Path(directory) / 'bin'
            result = subprocess.run(
                [str(ROOT / 'install.sh')],
                env={**os.environ, 'XDG_DATA_HOME': str(data_dir),
                     'XDG_BIN_HOME': str(bin_dir)},
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for size in (16, 22, 24, 32, 48, 64):
                icon = data_dir / 'icons/hicolor' / f'{size}x{size}' / 'apps/coderabbit-review-queue.png'
                with self.subTest(size=size):
                    image = QImage(str(icon))
                    self.assertFalse(image.isNull())
                    self.assertEqual(image.pixelColor(0, 0).alpha(), 0)


if __name__ == '__main__':
    unittest.main()

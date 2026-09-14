"""`python3 -m adb_screenshot` のエントリポイント。"""

import sys

from .cli import main

sys.exit(main())

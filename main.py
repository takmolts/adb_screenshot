#!/usr/bin/env python3
"""adb_screenshot の起動スクリプト（`python3 main.py --help`）。"""

import sys

from adb_screenshot.cli import main

if __name__ == "__main__":
    sys.exit(main())

"""PyInstaller entry point for ApexityEduBridge.exe."""
from __future__ import annotations

import sys

from apexity_bridge.__main__ import main

if __name__ == "__main__":
    sys.exit(main())

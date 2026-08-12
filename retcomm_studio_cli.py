#!/usr/bin/env python3
"""RetComM Studio entrypoint."""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from retcomm_studio.cli import main

if __name__ == "__main__":
    raise SystemExit(main())

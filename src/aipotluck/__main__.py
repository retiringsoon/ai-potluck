"""Support ``python -m aipotluck`` as an alias for the ``potluck`` console script."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())

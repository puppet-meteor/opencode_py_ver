"""``python -m opencode``: delegate to :func:`opencode.cli.main` (no own logic)."""

import sys

from opencode.cli import main

if __name__ == "__main__":
    sys.exit(main())

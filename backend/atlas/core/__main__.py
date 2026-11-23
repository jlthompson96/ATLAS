"""``python -m atlas.core``: run atlas-core (see ``atlas.core.service``)."""

from __future__ import annotations

import sys

from atlas.core.service import main

if __name__ == "__main__":
    sys.exit(main())

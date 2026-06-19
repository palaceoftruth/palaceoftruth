from __future__ import annotations

import sys
from pathlib import Path


# Allow `python backend/scripts/palaceoftruth_mcp.py` from the repo root without
# requiring a packaged install of the backend module first.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.mcp_server import main


if __name__ == "__main__":
    main()

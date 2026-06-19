from __future__ import annotations

import sys
from pathlib import Path


# Compatibility entrypoint for existing MCP configs. New Palace configs should
# prefer `backend/scripts/palaceoftruth_mcp.py`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.mcp_server import main


if __name__ == "__main__":
    main()

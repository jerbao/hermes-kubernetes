"""Repository entry point for the runtime MCP package manager."""

import sys as _sys
from pathlib import Path as _Path

if __name__ == "__main__" and "__file__" in globals():
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
from pathlib import Path
import sys

from hermes_kubernetes.mcp.updates import main


if __name__ == "__main__":
    raise SystemExit(main())

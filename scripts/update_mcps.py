"""Repository entry point for the runtime MCP package manager."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from mcp_update import main


if __name__ == '__main__':
    raise SystemExit(main())

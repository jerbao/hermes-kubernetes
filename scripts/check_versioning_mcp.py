"""Verify versioned MCP locks using an isolated package fixture, without external MCP calls."""
from pathlib import Path
import subprocess
import sys
root=Path(__file__).resolve().parents[1]
raise SystemExit(subprocess.run([sys.executable,'-m','unittest','discover','-s','mcp','-p','test_packages.py','-v'],cwd=root).returncode)

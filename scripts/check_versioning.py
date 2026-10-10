"""Run versioning acceptance once, with isolated homes and a local Git remote."""
import subprocess
import sys
from pathlib import Path
root=Path(__file__).resolve().parents[1]
raise SystemExit(subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-p','test_version_workspace.py','-v'],cwd=root).returncode)

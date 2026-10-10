"""Run focused skill lifecycle tests without network access or model calls."""
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
commands = [
    [sys.executable,'-m','unittest','discover','-s','tests','-p','test_skill_updates.py','-v'],
    [sys.executable,'-m','unittest','discover','-s','tests','-p','test_home_layout.py','-k','test_review_','-v'],
]
for command in commands:
    result = subprocess.run(command,cwd=root)
    if result.returncode: raise SystemExit(result.returncode)

"""Run the required project checks once, without production integrations or full upstream tests."""
import argparse
from pathlib import Path
import subprocess
import sys
root=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser(description=__doc__);p.add_argument('--runner-only',action='store_true');p.add_argument('--test',help='Run only this unittest filename pattern');a=p.parse_args()
commands=([[sys.executable,'-m','unittest','discover','-s','mcp','-p','test_runner.py','-v']] if a.runner_only else
 [[sys.executable,'-m','unittest','discover','-s','tests','-p',a.test,'-v']] if a.test else
 [[sys.executable,'scripts/validate.py'],[sys.executable,'-m','unittest','discover','-s','tests','-v']])
for command in commands:
 result=subprocess.run(command,cwd=root)
 if result.returncode:raise SystemExit(result.returncode)

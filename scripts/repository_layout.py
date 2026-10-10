"""Report top-level directories, tracked files and ignore rules without reading personal data."""
import json
from pathlib import Path
import subprocess
root=Path.cwd()
tracked=subprocess.check_output(['git','ls-files','-z'],text=True).split('\0')
rows=[]
for path in sorted(root.iterdir(),key=lambda p:p.name):
    if not path.is_dir():
        continue
    prefix=path.name+'/'
    files=[f for f in tracked if f.startswith(prefix)]
    ignored=subprocess.run(['git','check-ignore','-v','--no-index',prefix],capture_output=True,text=True)
    row={'directory':prefix,'tracked_files':len(files),'ignore_rule':ignored.stdout.strip() or None}
    if files:
        row['files']=files
    elif path.name=='.local':
        row['subdirectories']=sorted(p.name+'/' for p in path.iterdir() if p.is_dir())
    rows.append(row)
print(json.dumps(rows,ensure_ascii=False,indent=2))

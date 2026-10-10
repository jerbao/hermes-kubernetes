"""Review pending project paths and source before publication, without printing file contents."""
import ast
import json
from pathlib import Path
import subprocess
import sys

root=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(root/'runtime'),str(root/'scripts')]
from project_files import allowed,layout
from versioned_state import check_content,environment_secrets

def main():
    def git(*args):return subprocess.check_output(['git',*args],cwd=root,text=True).split('\0')
    names=set(git('diff','--name-only','-z','HEAD'))|set(git('ls-files','--others','--exclude-standard','-z'));names.discard('')
    value=layout(root);secrets=environment_secrets(root)
    for name in sorted(names):
        if not allowed(name,value):raise ValueError('Unclassified path: '+name)
        path=root/name
        if path.is_symlink():raise ValueError('Source symlink: '+name)
        if not path.is_file():continue
        data=path.read_bytes()
        try:check_content(data,secrets)
        except ValueError:raise ValueError('Content requires private review: '+name) from None
        if path.suffix=='.py':ast.parse(data,filename=name)
        elif path.suffix=='.json':json.loads(data)
    print(json.dumps({'status':'reviewed','changed_files':len(names),'paths':sorted(names)}))
if __name__=='__main__':main()

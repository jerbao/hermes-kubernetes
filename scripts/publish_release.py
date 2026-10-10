"""Publish the immutable distribution manifest after all component builds succeed."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'distribution'))
from release import COMPONENTS, dump, validate
from images import public_images


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--version',required=True);p.add_argument('--digests',type=Path,required=True);a=p.parse_args()
    repository=os.environ['GITHUB_REPOSITORY'];revision=os.environ['GITHUB_SHA']
    value=validate({'schema':1,'repository':repository,'version':a.version,'revision':revision,
                    'images':{c:(a.digests/(c+'.txt')).read_text().strip() for c in COMPONENTS}})
    public_images(value)
    existing=subprocess.run(['gh','release','view',a.version,'--repo',repository],capture_output=True)
    if existing.returncode==0:raise ValueError('Release already exists; immutable releases are never overwritten')
    manifest=a.digests/'distribution.json';manifest.write_text(dump(value))
    notes=a.digests/'notes.md';notes.write_text('Shared Hermes runtime and isolated MCP executors.\n\nSource: `'+revision+'`. Private instances adopt this version through a reviewed lock update.\n')
    subprocess.run(['gh','release','create',a.version,str(manifest),'--repo',repository,'--target',revision,'--title',a.version,'--notes-file',str(notes)],check=True)
    print(json.dumps({'released':a.version,'revision':revision}))


if __name__=='__main__':main()

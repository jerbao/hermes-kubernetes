"""Build from selected Git source, never send HERMES_HOME to the Docker daemon."""

import sys as _sys
from pathlib import Path as _Path

if __name__ == "__main__" and "__file__" in globals():
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
import argparse
import shutil
from pathlib import Path

from hermes_kubernetes.project.files import assert_paths, layout, tracked_files


def prepare(root, output, component="hermes"):
    if component not in {"hermes", "mcp"}:
        raise ValueError("Unknown image component")
    if output.exists():
        raise FileExistsError("Choose a new image context directory")
    value = layout(root)
    files = tracked_files(root)
    assert_paths(root, files, value)
    dockerfile = f"containers/{component}.Dockerfile"
    prefixes = (
        ("hermes_kubernetes/", "config/", "skills/")
        if component == "hermes"
        else ("hermes_kubernetes/executors/", "dependencies/mcp/")
    )
    selected = [
        name
        for name in files
        if name == dockerfile
        or name.startswith(prefixes)
        or component == "mcp"
        and name == "hermes_kubernetes/__init__.py"
    ]
    if dockerfile not in selected:
        raise ValueError("Stage the image source before building")
    output.mkdir(parents=True)
    for name in selected:
        destination = output / ("Dockerfile" if name == dockerfile else name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / name, destination)
    # The root deny-all rules make a direct build of HERMES_HOME fail without
    # transmitting state. This isolated context admits only the selected files.
    admitted = {"Dockerfile", "source-provenance.json", "install-stamp.json"}
    for name in selected:
        if name == dockerfile:
            continue
        admitted.add(name)
        admitted.update(str(parent) + "/" for parent in Path(name).parents if str(parent) != ".")
    (output / ".dockerignore").write_text(
        "*\n" + "".join("!" + name + "\n" for name in sorted(admitted))
    )
    return selected


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--component", choices=["hermes", "mcp"], default="hermes")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    files = prepare(root, args.output.resolve(), args.component)
    print(f"Image context prepared: {len(files)} project files")

"""Build from selected Git source, never send HERMES_HOME to the Docker daemon."""
import argparse
import shutil
from pathlib import Path

from project_files import assert_paths, layout, tracked_files


def prepare(root, output):
    if output.exists():
        raise FileExistsError("Choose a new image context directory")
    value = layout(root)
    files = tracked_files(root)
    assert_paths(root, files, value)
    selected = [name for name in files if name in {"Dockerfile", ".dockerignore"}
                or Path(name).parts[0] in {"runtime", "config", "skills"}]
    if "Dockerfile" not in selected:
        raise ValueError("Stage the image source before building")
    output.mkdir(parents=True)
    for name in selected:
        destination = output / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / name, destination)
    # The root deny-all rules make a direct build of HERMES_HOME fail without
    # transmitting state. This isolated context admits only the selected files.
    admitted = {"Dockerfile", "source-provenance.json", "install-stamp.json"}
    for name in selected:
        if name == ".dockerignore":
            continue
        admitted.add(name)
        admitted.update(str(parent) + "/" for parent in Path(name).parents if str(parent) != ".")
    (output / ".dockerignore").write_text("*\n" + "".join("!" + name + "\n" for name in sorted(admitted)))
    return selected


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    files = prepare(root, args.output.resolve())
    print(f"Image context prepared: {len(files)} project files")

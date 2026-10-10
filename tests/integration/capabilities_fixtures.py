"""Create synthetic plugin source for manual acceptance; no personal state."""

import argparse
from pathlib import Path
import subprocess


def create(directory):
    directory.mkdir(mode=0o755)
    repository = directory / "plugin"
    repository.mkdir(mode=0o755)
    (repository / "plugin.yaml").write_text("""name: lifecycle-fixture
version: 1.0.0
description: Synthetic plugin lifecycle acceptance fixture
python_dependencies:
  - pyfiglet>=1,<2
config_schema:
  greeting:
    type: string
    default: hello
""")
    (repository / "__init__.py").write_text("""import json
def register(ctx):
    def handler(args, **kwargs):
        from pyfiglet import figlet_format
        greeting=ctx.get_config('greeting', default='hello')
        return json.dumps({'greeting':greeting,'rendered':figlet_format(greeting)})
    ctx.register_tool(name='lifecycle_fixture',toolset='lifecycle_fixture',
        schema={'name':'lifecycle_fixture','description':'Render the configured synthetic greeting',
                'parameters':{'type':'object','properties':{}}}, handler=handler)
""")
    for args in [
        ("init", "-q"),
        ("config", "user.name", "Fixture"),
        ("config", "user.email", "fixture@example.invalid"),
        ("add", "."),
        ("commit", "-qm", "Synthetic plugin"),
    ]:
        subprocess.run(["git", "-C", str(repository), *args], check=True, capture_output=True)
    return directory


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    print(create(args.directory))

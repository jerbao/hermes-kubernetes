PYTHON ?= python3
IMAGE ?= hermes-personal:local
MCP_PYTHON ?= python3
SOURCE ?= hermes-agent

.PHONY: validate test test-hermes test-hermes-full test-mcp source checkout source-status build
validate:
	$(PYTHON) hermes_kubernetes/project/validation.py
	$(PYTHON) -m hermes_kubernetes.checks.structure
test:
	$(PYTHON) hermes_kubernetes/source/prepare.py --checkout --output "$(SOURCE)" --allow-local-edits
	$(PYTHON) hermes_kubernetes/source/skill_patches.py --source "$(SOURCE)"
	HERMES_SOURCE_DIR="$(abspath $(SOURCE))" $(PYTHON) -m unittest discover -s tests -t . -v
test-hermes:
	$(PYTHON) hermes_kubernetes/source/tests.py --source "$(SOURCE)"
test-hermes-full:
	$(PYTHON) hermes_kubernetes/source/tests.py --full --source "$(SOURCE)"
test-mcp:
	$(MCP_PYTHON) -m unittest discover -s tests/executors -t . -v
source:
	$(PYTHON) hermes_kubernetes/source/prepare.py
checkout:
	$(PYTHON) hermes_kubernetes/source/prepare.py --checkout --output "$(SOURCE)"
source-status:
	$(PYTHON) hermes_kubernetes/terminal/source_status.py --source "$(SOURCE)"
build:
	sh scripts/build_image.sh $(IMAGE)

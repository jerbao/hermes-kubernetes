PYTHON ?= python3
IMAGE ?= hermes-personal:local
MCP_PYTHON ?= python3
SOURCE ?= hermes-agent

.PHONY: validate test test-hermes test-hermes-full test-mcp source checkout source-status build
validate:
	$(PYTHON) scripts/validate.py
test:
	$(PYTHON) scripts/prepare_source.py --checkout --output "$(SOURCE)" --allow-local-edits
	$(PYTHON) scripts/check_official_skill_patches.py --source "$(SOURCE)"
	HERMES_SOURCE_DIR="$(abspath $(SOURCE))" $(PYTHON) -m unittest discover -s tests -v
test-hermes:
	$(PYTHON) scripts/test_native.py --source "$(SOURCE)"
test-hermes-full:
	$(PYTHON) scripts/test_native.py --full --source "$(SOURCE)"
test-mcp:
	$(MCP_PYTHON) -m unittest discover -s mcp -v
source:
	$(PYTHON) scripts/prepare_source.py
checkout:
	$(PYTHON) scripts/prepare_source.py --checkout --output "$(SOURCE)"
source-status:
	$(PYTHON) vm/source_status.py --source "$(SOURCE)"
build:
	sh scripts/build_image.sh $(IMAGE)

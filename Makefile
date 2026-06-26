.PHONY: dev install test clean

# Install in editable mode — picks up working-tree changes immediately.
# ALWAYS use this during development; never use pip install git+file://...
dev:
	pip install -e ".[dev]"

install:
	pip install .

test:
	pytest

clean:
	rm -rf dist/ build/ *.egg-info/ .pytest_cache/ __pycache__/

# Verify your editable install is wired correctly
check-install:
	@python -c "import bootstrap; print('bootstrap', bootstrap.__version__, '— editable install OK')"
	@agentOS --version

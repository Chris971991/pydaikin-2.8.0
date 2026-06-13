.PHONY: default format white black lint test check clean pypi

default: check

format: white
	isort bin/pydaikin pydaikin/*.py tests/*.py

white: black

black:
	black . pydaikin

lint:
	flake8

check: format lint

clean:
	rm -f *.pyc
	rm -rf .tox
	rm -rf *.egg-info
	rm -rf __pycache__
	rm -f pip-selfcheck.json
	rm -rf pytype_output

# Manual fallback only — the tag-triggered GitHub workflow builds and publishes
# releases. Release flow: bump pyproject.toml version -> commit -> git tag vX.Y.Z
# -> git push && git push --tags (see .claude/CLAUDE.md "Releasing pydaikin Updates").
pypi:
	rm -f dist/*
	python3 -m build
	twine upload dist/*

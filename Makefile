.PHONY: setup test verify build serve

setup:
	uv sync
	npm install

test:
	uv run pytest
	uv run ruff check .
	uv run ruff format --check .
	npm run check

verify:
	uv run python -m kelly_lab.verify

build:
	npm run build

serve: build
	npm run serve

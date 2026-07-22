.PHONY: setup test verify build build-local serve serve-local

setup:
	uv sync
	npm ci
	npm --prefix worker ci

test:
	uv run pytest
	uv run ruff check .
	uv run ruff format --check .
	npm run check
	npm --prefix worker test

verify:
	uv run python -m kelly_lab.verify

build:
	npm run build

build-local:
	npm run build:local

serve: build
	npm run serve

serve-local: build-local
	npm run serve:local

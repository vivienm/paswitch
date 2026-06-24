ci: lint audit test

lint: ruff ty typos

audit: uv-audit

test: pytest

ruff: ruff-check ruff-format

ruff-check:
    uv run ruff check

ruff-format:
    uv run ruff format --check

ty:
    uv run ty check

pytest *args="":
    uv run pytest {{args}}

uv-audit:
    uv audit

typos:
    uv run typos

run *args="":
    uv run paswitch {{args}}

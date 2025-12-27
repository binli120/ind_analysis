# Unit Tests

This document describes how unit tests are organized, named, and run for this
repo.

## Overview

- Test framework: `pytest`
- Unit tests live in two places:
  - `src/**/__test__/` (module-adjacent tests)
  - `tests/` (top-level tests)
- File naming conventions are configured explicitly in
  `scripts/run_unit_tests.sh`.

## Test Layout

Module-adjacent tests live beside the code they cover:

```
src/<package>/__test__/*.test.py
```

Top-level tests live in:

```
tests/test_*.py
```

Each `__test__` folder includes an `__init__.test.py` for consistent discovery
and to mirror existing patterns in the codebase.

## Naming Conventions

`pytest` is configured to find:

- `*.test.py`
- `test_*.py`

This is set in `scripts/run_unit_tests.sh` via:

```
-o python_files='*.test.py test_*.py'
```

## Running Unit Tests

Recommended (unit tests only):

```
./scripts/run_unit_tests.sh
```

This script:

- Sets `PYTHONPATH` to the repo root.
- Finds all `src/**/__test__/` directories dynamically.
- Runs those plus the `tests/` folder.

Manual `pytest` invocation:

```
poetry run pytest --import-mode=importlib \
  -o python_files='*.test.py test_*.py' \
  src tests
```

Run a single file:

```
poetry run pytest src/utils/__test__/utils.test.py
```

Run a single test by name:

```
poetry run pytest -k test_extract_event_time
```

## Adding New Unit Tests

1) Create a `__test__` folder next to the module you are testing.
2) Add `__init__.test.py` (empty module docstring is fine).
3) Add a new `*.test.py` file.
4) Use `pytest` fixtures and lightweight fakes/mocks for external services.

Example structure:

```
src/my_module/
  __test__/
    __init__.test.py
    my_module.test.py
```

## Mocking Guidelines

Avoid network calls in unit tests:

- S3: use fake clients (see `tests/test_s3_sync_service.py`).
- OpenAI: use dummy client responses (see `tests/test_ai_metadata.py`).
- Time-dependent logic: patch with `monkeypatch` or `freezegun`-style helpers.

## Troubleshooting

- Missing optional dependencies: some tests import optional libraries (e.g.
  `PIL`). Install the relevant extras via Poetry if a test fails on import.
- Import errors: ensure `PYTHONPATH` includes the repo root, or run via
  `./scripts/run_unit_tests.sh`.
- Discovery issues: verify file naming matches `*.test.py` or `test_*.py`.


# Contributing

Thanks for helping improve GitHub ↔ Gitee Bridge. Bug reports, documentation fixes,
regression tests and platform compatibility improvements are welcome.

## Development

Use Python 3.10 or newer and Git:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-bridge.txt -r requirements-dev.txt
.venv/bin/python -m pytest
```

Tests use fake API responses and temporary local Git repositories. They do not
require tokens or write to GitHub/Gitee. The retained upstream tests cover the
legacy entry points and shared Git utilities.

For integration testing, use dedicated disposable repositories. Never point a
test at a production repository: synchronization overwrites target refs with the
source refs. Keep credentials in environment variables or the ignored
`.env.bridge` file. State databases and private test fixtures belong under the
ignored `state/` directory.

## Pull requests

- Explain the user-visible problem and the resulting behavior.
- Include focused regression coverage for API contracts, object mapping or
  synchronization behavior that changes.
- Keep GitHub canonical unless a change explicitly defines a new conflict policy.
- Preserve create-intent recovery and fail on incomplete API listings. Do not
  blindly retry writes that may already have succeeded.
- Do not treat a successful mock test as evidence of live platform compatibility.
- Preserve upstream license notices when reusing code.

The main bridge lives in `bridge/`; shared upstream Git code is in `lib/`.
See [architecture](docs/bridge-architecture.md) and [operations](docs/operations.md).

## Reporting issues

Include the command used, Python/Docker version, sanitized error, expected
behavior and a minimal reproduction. State whether the repository is personal
or organizational and whether the PR is from a fork. Do not include access
tokens, webhook secrets, raw private payloads or state databases.

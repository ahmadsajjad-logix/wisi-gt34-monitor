# Dependency Audit - V11.3L

Source reviewed: production migration bundle created 2026-09-14.

## Findings

- Captured working Python version: `Python 3.14.6`.
- The production `gt34_monitor` package was present on the working PC but was omitted from the first sanitized GitHub checkpoint.
- The required production `gt34_monitor` source files included in this update use Python standard-library modules only.
- The working PC's global `pip freeze` contains many third-party packages. That file is preserved as `requirements-working-pc-freeze.txt` for environment reconstruction/reference, but those packages are not all requirements of this project.
- A historical/probe script (`probe_wisi_live.py`) uses `requests`/`urllib3`; it is not part of the current production runtime package added by this update.

## Production dependency policy

`requirements.txt` documents only mandatory third-party dependencies for the production runtime. At this checkpoint there are none. Project-local modules such as `gt34_monitor` are committed as source code.

## Server deployment

Use the same supported Python generation as the validated environment where practical. Before cutover, validate imports, monitoring, PRTG wrapper execution, database writes, and alarm logic on the target server.

# Repository work

Read README.md and the relevant docs before changing behavior. Preserve original content, project identity, request IDs, version checks, event history and human approval boundaries. Use synthetic fixtures, not personal state.

Make focused changes, run the relevant checks, and report the scope actually validated. The code index is derived; the project event log remains authoritative. Do not modify live event logs or databases to make a test pass. Do not auto-approve pending user requests.

For the maintainer workflow, builds, tests and batch processing run on the Windows worker; WSL hosts the Unix service tests. Do not assume a contributor has the maintainer's SSH aliases or paths. Never add personal host configuration to source.

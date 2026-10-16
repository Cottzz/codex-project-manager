# Validation for 0.1.0-alpha.1

The release candidate was checked on a Windows worker: Ubuntu WSL/Python 3.12.3 for the Unix core and native Windows/Node for frontend contracts. [Structured receipt](release-verification.json) binds the relevant source hashes.

- 156 distinct backend/parser cases: the first run passed155 and found one obsolete product-name assertion. After correction, all26 cases in its module and both clean-install/cache-import cases passed. Unchanged passing results were reused; this is not represented as a second complete156-case run.
- 53 frontend/configuration checks passed on Windows.
- New synthetic workspace, CLI/actual HTTP identity, restart persistence and preserved human gate were exercised. First cache import rejects a foreign identity/incompatible schema and preserves unrelated pointers.
- README screenshot comes from the actual candidate service and the shipped synthetic demo. No personal project snapshot is included.

GitHub Actions separately runs Python syntax and the53 native Windows Node checks. Its result is visible on the Actions page; it does not pretend to run the Unix service on native Windows. Optional configured SSH refresh has configuration/cache-invariant tests, but a fresh full remote round-trip is not claimed for this release.

Earlier private UI prototype counts are not included here. Complete accessibility, long-duration memory, sustained comfort, external Codex version compatibility and user acceptance remain separate. Initial test warnings and the old-name failure were retained in the maintainer's verification record.

# Validation for 0.1.0-alpha.3

[Structured receipt](release-verification.json) binds this release to its unchanged application source and presentation assets.

- This release re-ran 3 coherent-example tests and 2 clean-install/cache-import tests on Windows / Ubuntu WSL. The example has 16 tasks and preserves its pending human gate, source-version difference and existing-directory refusal.
- All 53 frontend/configuration tests passed on native Windows Node.
- Application Python, browser logic, styles and font hashes are unchanged from alpha.2. The 156 backend and 42 real-browser checks in [alpha.2 validation](validation-alpha.2.md) are reused for that unchanged scope; they were not all re-run for alpha.3.
- The approved presentation was generated on Windows from the actual service: full screenshots, clearly labeled explanatory diagrams, a 45-second recording and two caption tracks. Its review process checked 121 local review-page conditions, 10 first-use checks, separate user-directory readback and video playback. Those counts do not represent a public GitHub usability study.
- Approval covers the homepage and demonstration materials only. No model, real delivery or human approval was invoked for the synthetic project. The first 3 sample-task completions only verify sample files, not business implementation.
- Packaging checks every relative Markdown reference and source-file SHA, excludes project state and private environment bindings, and extracts the archive for a new example/CLI check. Public download and GitHub page readback are checked after release.

GitHub Actions separately runs the named Windows syntax and 53 Node checks. See its result for the exact published commit. Native Windows service, full screen-reader coverage, sustained comfort, long-term memory behavior and external Codex versions remain outside this release verification.

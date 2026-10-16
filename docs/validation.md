# Validation for 0.1.0-alpha.2

[Structured receipt](release-verification.json) binds the tested source and full Simplified Chinese font hashes.

- 156 backend/parser tests passed in a complete run on the Windows worker using Ubuntu WSL/Python 3.12.3.
- 53 frontend/configuration checks passed on native Windows Node. Initial failures came from changed copy assertions and a fixture missing the new status formatter; these were corrected before the final run.
- 42 real-service browser checks passed on Windows Chrome: nine views, fourteen creation-form schemas/cancel paths, source reading, task dependencies, missing-index boundary, Simplified Chinese font loading, persisted event labels, actual UI task creation, CLI readback, refresh persistence and unchanged human gate. Screenshots were captured before the temporary UI-write check.
- Five workflow screenshots use the shipped synthetic demo against the actual Python HTTP server. No private project data, mocked Store or real model invocation is involved. This small demo does not establish dense-project performance.
- The complete font was converted to WOFF2 on Windows, preserving all30,890 mapped codepoints. Its OFL notice is included. Font loading passed; exhaustive visual inspection of every glyph is not claimed.
- The Windows-built ZIP is checked for file hashes, documentation links and excluded private bindings, then extracted for a fresh-project CLI smoke check.

GitHub Actions independently runs Python syntax and53 native Windows Node checks. It does not run the Unix service on native Windows. The Actions result must be checked for the specific commit.

Earlier prototype test counts are not reused as public live-service proof. Optional configured SSH refresh, external Codex versions, new real approvals, complete accessibility, long-duration memory and sustained comfort remain separate verification scopes. Human acceptance is not inferred from passing tests.

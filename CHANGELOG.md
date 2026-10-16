# Changelog

## 0.1.0-alpha.3 — 2026-10-16

- Publish the reviewed English and Simplified Chinese introduction and three connected project workflows.
- Add a reproducible 16-task notification-release example with dependencies, evidence revisions and an unresolved human gate.
- Add actual application screenshots, a 45-second recording and both caption tracks. Explanatory diagrams are labeled separately.
- Separate application and project directories in first-use instructions; keep the example in a user directory.
- Application UI, runtime logic and approval boundaries remain unchanged from alpha.2.


## 0.1.0-alpha.2

- Ship the white-content / light-gray-canvas UI, Simplified Chinese Noto font, consistent typography and reduced decorative icons.
- Retain application routing, task gates and original project content; remove prototype-only messaging and endpoints.
- Add five actual-service workflow screenshots and explicit repository presentation references.
- Validate static assets, CLI/HTTP persistence, and released UI on Windows/Ubuntu WSL.


## 0.1.0-alpha.1 — 2026-10-16

First public source release of Codex Project Manager.

- Persistent project graph with tasks, dependencies, evidence, decisions, artifacts and human gates.
- Local browser workspace, CLI and stdio MCP using the same store.
- Explicit multi-project routing, bounded source/history reads and separate managed Codex chats.
- Optional deterministic code index and explicit worker configuration; removed personal host/project bindings from the distribution.
- Synthetic demo, bilingual quick start, contribution/security documentation and release checks.

The public package preserves the current application behavior. The A white redesign remains under review. See docs/validation.md and docs/limitations.md for the exact checked scope.

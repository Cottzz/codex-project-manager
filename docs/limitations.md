# Known limits

- Public alpha with the white-content / light-gray-canvas UI. Visual release does not establish complete accessibility or user acceptance.
- Unix file locks: macOS/Linux service; Windows through Ubuntu WSL. Native Windows service is unsupported.
- Single trusted local user; loopback binding, no hosted tenancy or remote multi-user authorization.
- Existing Codex desktop chats are read-only sources, not transparently resumed/shared-control sessions.
- Human approvals are never inferred from model output or test counts.
- Graphs, context and source readers are bounded; results expose truncation. The initial fit-all graph may have small labels.
- Code analysis is a derived static candidate index. Dynamic dispatch, unsupported constructs and parser errors remain unknown. Optional worker provisioning is explicit, never automatic.
- Long-duration memory behavior, complete screen-reader conformance and sustained reading comfort are not comprehensively validated.
- Public CI covers its declared subset. Recorded Windows/WSL integration receipts do not imply every operating system or external Codex version passed.

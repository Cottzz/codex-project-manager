# CLI, MCP and Codex

The core works without a model account. Read schemas with `python3 pgw.py tools`; all routes require an explicit initialized project root for durable operations.

Example stdio MCP configuration (replace paths with your own absolute paths):

```json
{"mcpServers":{"codex-project-manager":{"command":"python3","args":["/absolute/codex-project-manager/pgw.py","--root","/absolute/project","mcp"]}}}
```

Your MCP host is responsible for enabling this server. The application does not silently edit agent configuration. Verify returned project identity on first use.

Optional chat discovery and managed execution require a working `codex` executable, account/session and the corresponding App Server protocol. Desktop chats are discoverable/linkable sources; shared control of a running desktop chat is not supported. Managed chats created by the workspace are separate. Approvals and user-input requests retain their exact IDs. Disconnection can leave an outcome unknown; reconcile before retrying.

Codex is an optional external product with its own terms, authentication and usage costs. Installing this repository does not install Codex or call a model automatically.

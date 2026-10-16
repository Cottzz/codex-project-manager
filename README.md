# Codex Project Manager

[![Windows checks](https://github.com/Cottzz/codex-project-manager/actions/workflows/checks.yml/badge.svg)](https://github.com/Cottzz/codex-project-manager/actions/workflows/checks.yml) · [MIT](LICENSE)

**Local-first project management for Codex and AI coding workflows.**

Keep tasks, dependencies, decisions, evidence and chat references in one persistent project graph. Continue a project across conversations without treating a chat summary as the source of truth.

[Download alpha](https://github.com/Cottzz/codex-project-manager/releases/tag/v0.1.0-alpha.2) · [简体中文](README.zh-CN.md) · [Quick start](#quick-start) · [User guide](docs/user-guide.md) · [Architecture](docs/architecture.md) · [Roadmap](docs/roadmap.md)

![Current workspace with synthetic demonstration data](docs/images/workspace.png)

The released application uses the white-content / light-gray-canvas interface shown here. Screenshots use the shipped synthetic project against the actual local service. [Explore five workflows](docs/workflows.md).

## Who it is for

You use Codex for work that spans multiple conversations and need to answer: **What is ready? What blocks it? What changed? Where is the evidence?** This workspace keeps those answers on disk and makes them available through a browser, CLI and MCP.

It is an independent community tool, not an official OpenAI product. It is not a code editor, a hosted team service, or an autonomous approval system.

## What you can do

| Need | Available workflow |
|---|---|
| Resume a large project | Read the current task frontier, dependencies and durable decisions. |
| Keep several projects separate | Attach explicit project directories; each has its own event history. |
| Verify results | Bind evidence and checks to source versions; completion respects dependencies and human gates. |
| Understand change impact | Follow bounded task/evidence relationships; optionally query a deterministic code index. |
| Connect Codex work | Discover and link existing chats; control separately managed App Server chats when configured. |
| Use scripts or agents | Query and update the same state through CLI or stdio MCP. |

## Quick start

**Requirements:** Python 3.11+ on macOS or Linux. Windows users can run the service inside Ubuntu WSL; native Windows service execution is not supported because the store uses Unix file locks. The current UI is primarily Simplified Chinese; English and Chinese quick-start documentation are provided. Core project management uses the Python standard library. Codex and code-index tools are optional.

```sh
git clone https://github.com/Cottzz/codex-project-manager.git
cd codex-project-manager
python3 pgw.py --root "$PWD" init --name "My project" --goal "Ship a verified result"
python3 pgw.py --root "$PWD" serve --port 8766
```

Open **http://127.0.0.1:8766/**. Keep the terminal running; use Ctrl-C to stop. Re-run the serve command to resume. Do not initialize an existing project again. Use another port if 8766 is occupied.

To explore safely, create a synthetic project in a new directory:

```sh
python3 scripts/create_demo.py /tmp/cpm-demo
python3 pgw.py --root /tmp/cpm-demo serve --port 8767
```

The demo contains tasks, dependencies, evidence and a human gate. It does not start Codex, send messages, or approve anything.

## CLI and MCP

```sh
python3 pgw.py --root /absolute/project/path call project_status
python3 pgw.py --root /absolute/project/path call current_frontier
python3 pgw.py tools
```

Use the schemas returned by `tools`. Mutations carry a stable `request_id`; versioned updates require `expected_version`. A retry keeps the same ID and payload. Human approvals remain explicit.

[Configure stdio MCP](docs/integrations.md) · [Optional code indexing](docs/code-index.md) · [Data and recovery](docs/data-and-recovery.md)

## Release status and boundaries

This is a **public alpha**, not a claim of production maturity. Read [validation scope](docs/validation.md) and [known limits](docs/limitations.md) before relying on a workflow. Existing desktop chats can be discovered and linked; this does not grant shared control of a running desktop chat. Ended turns do not automatically complete tasks. Search and graphs are bounded and show truncation.

The service binds to loopback and assumes a single trusted local user. Do not expose it directly to the Internet. There is no hosted authentication, multi-user authorization, or SLA. Project data stays in your chosen directory; optional Codex/SSH actions have their own explicitly configured data paths.

## Contribute

Start with [CONTRIBUTING.md](CONTRIBUTING.md). Include reproduction steps, the relevant source version and actual validation. See [SECURITY.md](SECURITY.md) for vulnerability reporting. No personal project state or chat exports belong in issues or pull requests.

## License

[MIT](LICENSE). Optional third-party tools retain their own licenses; see [third-party notes](docs/third-party.md).

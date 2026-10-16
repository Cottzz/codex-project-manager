# Optional deterministic code index

The project graph works without a code index. Indexing is a separate derived cache. Run parser/batch work on an explicitly configured Windows worker; the service does not silently parse the repository on macOS.

## Build and first import

Install `requirements-index.txt` in a dedicated worker environment. Stage only explicitly authorized source files and note their original absolute source root and project ID (`call project_status`). On the worker:

```sh
python3 scripts/code_index_builder.py --input-root /staged/source --original-root /absolute/original/source --project-id PROJECT_ID --output index.sqlite
```

Transfer the result and compare its SHA256. On the Unix service host, explicitly install it:

```sh
python3 scripts/install_code_index.py --root /absolute/project --source-root /absolute/original/source --cache /downloaded/index.sqlite
python3 pgw.py --root /absolute/project code status
```

The installer checks SQLite integrity and exact project/source identity, retains unrelated project pointers and does not change task events. A compatible cache is not proof that dynamic relationships are complete.

## Explicit incremental worker configuration

Copy `worker-config.example.json` to the application checkout's `.project-graph/worker-config.json`, or select a file through `PGW_WORKER_CONFIG`. Configure only a trusted SSH alias with strict host-key checking, expected hostname, existing Windows/WSL work roots, Python executable and parser dependency path. Replace the example project ID and absolute source roots. No worker credentials are bundled.

```sh
python3 pgw.py --root /absolute/project code refresh '{"action":"start","request_id":"source-update-001","worker":"my-windows-worker"}'
python3 pgw.py --root /absolute/project code refresh '{"action":"status","request_id":"source-update-001","worker":"my-windows-worker"}'
python3 pgw.py --root /absolute/project code refresh '{"action":"apply","request_id":"source-update-001","worker":"my-windows-worker"}'
```

The initial cache must already exist. No automatic resubmission follows a lost connection. A changed source snapshot, pointer, request, result project or SHA prevents replacement. Worker paths currently accept simple absolute paths without spaces or shell delimiters; this is a declared alpha restriction. Provisioning SSH, Python, PowerShell 7, WSL, ripgrep and parser packages is manual. Do not add real worker configuration to Git.

The static index covers configured source languages, with unresolved/dynamic cases and limits explicitly recorded. Results are impact candidates and cannot complete tasks or approve results. Syntax parsers do not prove full semantic recall.

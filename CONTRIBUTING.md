# Contributing

Open an issue with the problem, current behavior, expected result, OS/runtime versions and minimal synthetic reproduction. Discuss changes to storage, approval boundaries or public APIs before implementing them. Keep one purpose per pull request.

Run relevant checks in an isolated environment. Preserve project identities, original content, request IDs, later events and human decisions. Do not use real personal project data in fixtures. Include screenshots for visual changes and state what is simulated versus real.

Core checks on a Unix environment:
```sh
python3 -m unittest discover -s tests -p 'test_*.py'
```
The optional parser suite needs `requirements-index.txt`. Frontend checks use Node 22+:
```sh
node --test tests/test_code_refresh.mjs tests/test_runtime_ui.mjs tests/test_view_sync.mjs tests/test_release_config.mjs
```

This project's maintained release verification runs on a Windows worker (Ubuntu WSL for Unix core; native Node for frontend). CI documents a smaller native Windows subset. Report actual commands and failures; do not equate a test count with human acceptance.

AI-assisted contributions are welcome when the contributor understands and verifies the change. Contributions are submitted under the repository MIT license; do not include third-party code without compatible rights and attribution.

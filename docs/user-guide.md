# User guide / 使用说明

Start with the [notification-release walkthrough / 发布通知连贯教程](presentation/story.md) to follow the current example. The principles below apply to your own projects.

1. **Choose a project / 选择项目**: confirm the active project before writing. Attach only an explicit existing directory. Initializing it creates empty state, not verified history.
2. **Read the overview and frontier / 总览与前沿**: tasks may be ready, in progress, blocked, waiting for human input, or waiting for validation. Read the reason and dependencies before acting.
3. **Open a task / 任务详情**: inspect source, relations and checks. Claim, update progress or record a blocker. A produced file does not complete the task.
4. **Add evidence / 登记依据**: point to an actual file or URL and a precise locator. A file hash binds the version, not the truth of its contents.
5. **Read relationships / 关系图**: B depends_on A means A is a prerequisite of B. Graphs and impact queries are bounded; a missing result does not prove no relation exists. Zoom or use object details when fit-all text is small.
6. **Connect chats / 聊天关联**: discovery and history preview are read-only until an explicit import/link action. Imported assistant statements are not verified results. Managed execution requires configured Codex and exact request identities.
7. **Handle decisions / 人工决定**: inspect the exact task/chat/turn/request and offered options. No script should infer approval from technical PASS.
8. **Recover from a conflict / 版本冲突**: preserve your intended input, refresh the object, compare the new version and submit a new intentional update. Do not reuse an idempotency key with a changed payload.

Project switching changes the action target. Search, source readers, empty states, errors and truncation must remain visible. Use `python3 pgw.py tools` for the exact current API contracts rather than copying stale mutation examples.

# Codex Project Manager

[MIT](LICENSE) · [v0.1.0-alpha.3](https://github.com/Cottzz/codex-project-manager/releases/tag/v0.1.0-alpha.3) · [Windows checks](https://github.com/Cottzz/codex-project-manager/actions/workflows/checks.yml)

### 让 Codex 项目跨对话继续推进。

不用每次从旧聊天重新拼出计划。把下一步任务、依赖、决定、证据和聊天关联放在同一个本地工作区，清楚知道哪些工作还需要人工决定。

[English](README.md) · [先体验示例](#先体验示例) · [接入自己的项目](#接入自己的项目) · [使用说明](docs/user-guide.md)

[![项目总览：可执行任务、依赖阻塞和人工确认](docs/presentation/overview-focus.png)](docs/presentation/overview-full.png)

*实际运行的白系新版界面，内容来自随附的“发布通知”合成项目。点击图片可查看完整工作区。*

[观看短演示](docs/presentation/walkthrough.mp4) · [跟随同一个项目操作](docs/presentation/story.md)

[下载视频](https://github.com/Cottzz/codex-project-manager/releases/download/v0.1.0-alpha.3/walkthrough.mp4) · [简体中文字幕](docs/presentation/walkthrough.zh.vtt) · [English captions](docs/presentation/walkthrough.en.vtt)

## 当项目超过一个聊天窗口

计划在一个对话里，实现在另一个对话里，评审又开了新对话。继续之前，你需要找到现在能做的任务、弄清阻塞原因，并确认此前依据是否还有效。

Codex Project Manager 将这些记录保存在本地。网页、CLI 和 MCP 读取同一份项目状态。内容需要明确登记或同步；它不会自动捕获所有对话，也不会接管正在运行的桌面聊天。

它适用于需要跨对话接续和追溯决定的项目。一次问答或简短个人待办，可能无需这套管理方式。

## 用三条流程推进项目

### 1. 找到下一任务，先核前置条件

进入任务看板，选择任务，查看依赖、必要检查与原始内容。“进行中”“待验证”和“待人工处理”分别记录。

[![任务前置条件与可用操作](docs/presentation/task-focus.png)](docs/presentation/tasks-full.png)

### 2. 看清受阻工作在等待什么

沿明确的依赖找到前置任务。下图仅解释 N03–N05 的实际关系，是说明图，不是图谱画布截图。图关系帮助定位相关工作，不代表图外不存在其他关系。复杂全图可能需要放大并结合对象详情阅读。

[![基于案例实际关系绘制的依赖说明图，非应用截图](docs/presentation/dependency-story.png)](docs/presentation/graph-full.png)

### 3. 接受结果前，回到原始依据

打开原文件，核对登记版本与当前版本。来源变化后需要重新审查。文件生成或聊天结束，不会自动完成任务，也不会批准发布。

[![原始来源已变化，保留版本差异提示](docs/presentation/source-focus.png)](docs/presentation/source-full.png)

示例包含 3 个阶段、16 个任务、明确依赖、实际样例文件、未知检查和一个待人工门禁。前三项完成只代表样例文件检查通过，不代表业务功能已经实现。[查看案例与预期状态](docs/presentation/story.md)。

## 先体验示例

**环境要求：** Python 3.11+，macOS 或 Linux。Windows 使用 Ubuntu WSL；核心服务依赖 Unix 文件锁。基础项目管理仅使用 Python 标准库。此示例不需要模型账号；随附简体字体采用 OFL 许可。

在终端运行：

```sh
git clone https://github.com/Cottzz/codex-project-manager.git
cd codex-project-manager
python3 examples/create_walkthrough.py "$HOME/cpm-notification-demo"
python3 pgw.py --root "$HOME/cpm-notification-demo" serve --port 8767
```

打开 **http://127.0.0.1:8767/**。进入 **任务看板**，打开 **N05**，查看它为什么等待 **N04**。再进入 **证据与判断**，选择 **E04**，点击 **查看原始来源**，核对原文件变化。

示例拒绝已有目录；请换一个新路径，不要覆盖。终端需保持运行，Ctrl-C 停止服务。恢复时只重新运行最后一条 serve 命令。示例不调用 Codex、不发送通知，也不批准待人工门禁。

## 接入自己的项目

将工具源码目录与被管理的项目目录分开。选择已有项目目录；仅在尚未接入时初始化一次：

```sh
python3 /absolute/codex-project-manager/pgw.py --root /absolute/my-project init --name "My project" --goal "Ship a verified result"
python3 /absolute/codex-project-manager/pgw.py --root /absolute/my-project serve --port 8766
```

打开 **http://127.0.0.1:8766/**，写入前确认项目名。初始化建立空状态，不会将已有文件或聊天自动导入为已验证历史。已有状态的项目只需 serve。端口已占用时改用其他端口，不要停止无关服务。

项目状态保存在所选目录的 `.project-graph/` 中，证据原文件仍在原位置。[备份与恢复](docs/data-and-recovery.md)。

## 从 CLI 或 MCP 操作

```sh
python3 /absolute/codex-project-manager/pgw.py --root /absolute/my-project call project_status
python3 /absolute/codex-project-manager/pgw.py --root /absolute/my-project call current_frontier
python3 /absolute/codex-project-manager/pgw.py tools
```

写入使用工具返回的实际参数合同，保留项目身份、请求 ID 和版本检查；人工批准仍须明确决定。[MCP 配置](docs/integrations.md)。

## 可选连接与当前边界

|能力|适用范围|
|---|---|
|已有 Codex 聊天|配置 Codex CLI 后可发现、预览和关联；不共享控制正在运行的桌面聊天。|
|工作区受管聊天|独立 App Server 会话；保留准确请求身份和人工审批。|
|代码关系|需单独配置确定性索引；返回静态候选、有界查询和明确未知项。[配置说明](docs/code-index.md)。|
|多个项目|各自持久状态独立，操作前核对当前选中项目。|
|本地使用|单个可信用户、回环服务；没有托管多用户权限系统。|

当前为公开 **alpha**，不是 OpenAI 官方产品。完整无障碍、长期阅读舒适度与长期内存行为尚未全面验证。见[已知限制](docs/limitations.md)与[版本验证](docs/validation.md)。CI 徽章仅代表对应工作流的检查，不覆盖全部能力。

## 了解、反馈与参与

[使用说明](docs/user-guide.md) · [架构](docs/architecture.md) · [路线图](docs/roadmap.md) · [贡献指南](CONTRIBUTING.md) · [报告问题](https://github.com/Cottzz/codex-project-manager/issues/new/choose) · [安全报告](SECURITY.md)

应用代码采用 [MIT](LICENSE)，第三方组件保留各自许可。[第三方说明](docs/third-party.md)。

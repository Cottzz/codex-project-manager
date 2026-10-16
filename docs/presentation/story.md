# 发布通知：跟随一个项目找到下一步 / Notification-release walkthrough

这是合成项目。没有真实用户数据、消息发送、模型调用或人工批准。The project is synthetic: no real user data, delivery, model execution or approval.

|步骤 / Step|操作 / Action|应看到 / Expected result|
|---|---|---|
|当前状态 / Overview|打开项目总览 / Open overview|明确合成项目名与不同状态的任务 / Project identity and distinct task states|
|依赖 / Dependency|任务看板 → N05 → 关联 N04 / Board → N05 → linked N04|N05等待未完成的接口任务；没有直接完成按钮绕过前置 / N05 waits for its prerequisite|
|实际图 / Graph|关系图 → 任务图 / Graph → tasks|明确有向依赖；可放大、定位对象；图外关系不保证完整 / Explicit edges; zoom for detail; unrecorded links remain unknown|
|依据 / Source|证据与判断 → E04 → 查看原始来源 / Evidence → E04 → read original|登记的版本1与当前版本2不同；需重核 / Registered v1 differs from current v2|
|人工边界 / Gate|任务看板 → N14 / Board → N14|G01待人工确认，前置尚未满足；本演示不作决定 / G01 remains pending; no decision is submitted|
|聊天 / Chats|聊天与运行 / Chats and runs|未配置实际连接时如实显示不可用；没有虚构聊天 / Unavailable connection is explicit; no fabricated thread|

演示中的 N01–N03 完成仅指样例文件存在和字段检查通过。N04/N07进行中；N05/N06/N10/N15受依赖约束；N11/N13待验证；N14待人工；N16暂缓。最终前沿由 Store 计算，完整检查结果见审核回执，不手改图日志。

Only N01–N03 complete actual sample-file checks, not business functionality. N04/N07 are in progress; N05/N06/N10/N15 depend on unfinished work; N11/N13 await verification; N14 awaits a human; N16 is deferred. The real Store computes the frontier.

[完整总览 / Overview](overview-full.png) · [任务 / Tasks](tasks-full.png) · [关系图 / Graph](graph-full.png) · [来源 / Source](source-full.png) · [聊天 / Chats](chats-full.png) · [录屏 / Video](walkthrough.mp4)

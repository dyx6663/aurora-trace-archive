# 设计说明：为什么 AURORA TRACE 不像普通 Coding Agent

## 1. 观察到的常见路线

公开的编程智能体通常集中在几种范式：

- SWE-agent：围绕软件工程 Issue，通过终端和编辑工具自动修复代码；
- OpenHands：提供更完整的开放式软件工程 Agent 环境；
- Aider：以终端为中心，用模型协助编辑 Git 仓库；
- ReAct 类方法：让模型在“思考—行动—观察”之间循环。

这些路线验证了工具调用闭环的有效性，但如果只展示聊天记录、终端输出或最终 Diff，很难判断每一步为什么发生。

## 2. 本项目的差异化定位

AURORA TRACE 将“证据链”作为一等对象，而不是运行结束后的日志附属物。每个事件同时记录：

```text
decision → tool call → local result → verification evidence
```

因此项目演示的中心不是“模型很聪明”，而是：

> Agent 的每个外部动作都能被解释、追踪和复盘。

这样可以从事件卡片、工具参数、Diff 和测试结果直接检查系统是否真的完成了任务。

## 3. 三个核心设计决策

当前版本进一步加入 Acceptance Contract。任务启动时，系统把“必须观察到什么证据”显式化为四个 Gate：基线故障、最小补丁、回归测试通过和工作区边界安全。最终 Confidence Score 由这些真实事件计算，而不是由模型自报。

### Evidence Ledger

运行过程中同时维护完整事件流和精简账本。事件流用于界面回放，账本用于回答“调用了哪些工具、修改了哪些文件、是否完成验证、谁批准了高风险操作”。

### Run Workspace

每次运行都复制到 `.runs/<run_id>`，不直接污染种子项目，也便于保持可重复的初始状态并计算 Diff。

对于导入的真实项目，隔离执行不是终点。Run 通过验收后，系统自动比较运行开始时记录的源项目清单，确认原项目没有被外部改动，再写回变更并保留写回前备份。用户不需要重复确认 Agent 是否已经发现问题；只有检测到源项目被外部改动时，系统才会暂停自动写回，避免覆盖新内容。

### Guarded Executor

文件操作经过路径边界检查；命令采用 `shell=False`，只允许 Python、pytest、npm、node 前缀，并限制超时。Agent 的能力因此是“可执行但可控”的。

### Trace Export

每个 run 同时保留内存事件流、`evidence.ndjson` 和可下载 JSON。前者服务于实时界面，后两者服务于复盘和运行审计。

### Approval Gate 与 Cancellation

写文件、精确替换和命令执行属于会改变工作区或产生外部副作用的操作。运行可以选择自动授权或手动授权；手动模式会把工具、参数和执行阶段写入待审批状态，只有明确批准后才调用执行器。拒绝会以结构化工具结果返回给 Agent，取消则会唤醒审批等待，并通过协作式检查中断正在运行的命令。

这两个机制共同形成运行时治理边界：模型可以提出动作，但不能绕过人的授权；用户可以在长任务或风险判断改变时终止 Run。终止结果与审批决定都会进入 Evidence Ledger，并区分 `COMPLETED`、`FAILED` 和 `CANCELLED`。

## 4. 参考入口

- SWE-agent: <https://github.com/SWE-agent/SWE-agent>
- OpenHands: <https://github.com/All-Hands-AI/OpenHands>
- Aider: <https://github.com/Aider-AI/aider>
- ReAct: <https://arxiv.org/abs/2210.03629>
- SWE-bench: <https://www.swebench.com/>

以上链接用于了解公开技术路线；本项目没有复制这些项目的代码或依赖其 Agent 框架。

# 从一次请求开始读代码

这个项目难读的一个主要原因，是“模型如何回答”和“后台任务如何可靠执行”曾混在同一个大文件里。现在可以先读单轮执行顺序，再按要修改的行为进入相应模块。

## 先区分几个概念

| 概念 | 表示什么 | 定义位置 |
| --- | --- | --- |
| Thread | 长期会话及其消息、用户和目标上下文 | [`core/threads.py`](../redis_sre_agent/core/threads.py) |
| Task | 一次后台执行的状态、进度、结果和审批信息 | [`core/tasks.py`](../redis_sre_agent/core/tasks.py) |
| TurnScope | 这一轮允许使用的目标范围 | [`core/turn_scope.py`](../redis_sre_agent/core/turn_scope.py) |
| PreparedTarget | 单轮编排中传递的已准备目标及兼容字段，不是新存储模型 | [`core/turn_targeting.py`](../redis_sre_agent/core/turn_targeting.py) |
| AgentResponse | agent 返回的回答、检索结果与工具证据 | [`agent/models.py`](../redis_sre_agent/agent/models.py) |
| Tool envelope | 工具执行记录，既参与推理，也用于回答后的证据追踪 | [`agent/models.py`](../redis_sre_agent/agent/models.py) 的 `ResultEnvelope` |

`thread_id`、`task_id` 和审批恢复使用的 graph/checkpoint 标识各有用途，不能把它们随意合并为一个 ID。

## 建议阅读顺序

1. 打开 [`core/turn_runner.py`](../redis_sre_agent/core/turn_runner.py) 的 `run_agent_turn`。先看调用顺序和提前返回条件：加载会话 → 准备目标 → 路由/发现 → 准备消息 → 执行 → 处理取消/审批 → 保存结果。
2. 打开 [`core/agent_execution.py`](../redis_sre_agent/core/agent_execution.py)。这里解释选哪个 agent、传什么上下文和历史消息、knowledge 模式何时使用缓存，以及怎样转换返回值。
3. 看 [`agent/chat_agent.py`](../redis_sre_agent/agent/chat_agent.py) 的 `_build_workflow` 和 `process_query`。先理解模型发出工具调用、工具结果回到模型、最终生成回答的循环。技能输出约束集中在 [`agent/skill_contracts.py`](../redis_sre_agent/agent/skill_contracts.py)。
4. 需要理解深入诊断时，再读 [`agent/langgraph_agent.py`](../redis_sre_agent/agent/langgraph_agent.py) 的 `_build_workflow`、`_process_query` 和 `process_query`。诊断证据的预处理、推理和最终输出比 chat 多，因此仍是较大的模块。
5. 遇到具体工具调用，再进入 [`tools/manager.py`](../redis_sre_agent/tools/manager.py) 和对应 provider。这里是工具策略、参数、执行和审批中断的边界。

## 后台执行模块怎么分工

| 模块 | 负责的行为 | 修改时重点验证 |
| --- | --- | --- |
| [`core/docket_tasks.py`](../redis_sre_agent/core/docket_tasks.py) | 任务注册、重试包装、兼容问答入口及其他后台任务 | 任务名、参数、返回格式保持稳定 |
| [`core/turn_runner.py`](../redis_sre_agent/core/turn_runner.py) | 单轮执行顺序、提前返回、异常上报和 trace 生命周期 | 成功、失败、取消、审批中断 |
| [`core/turn_targeting.py`](../redis_sre_agent/core/turn_targeting.py) | 客户端/会话目标优先级、临时实例、目标发现、权限检查 | 切换目标时清除旧范围；越权目标不执行 |
| [`core/agent_execution.py`](../redis_sre_agent/core/agent_execution.py) | 构造并调用 agent，知识模式预算与缓存策略 | 历史消息、目标上下文、缓存与迭代参数 |
| [`core/turn_fanout.py`](../redis_sre_agent/core/turn_fanout.py) | 多目标深入诊断的子任务和结果汇总 | 每个目标使用独立 agent、上下文与会话 |
| [`core/turn_completion.py`](../redis_sre_agent/core/turn_completion.py) | 会话保存、任务完成、引用、证据、完成事件 | 取消不能被成功覆盖；旧结果结构保持兼容 |
| [`core/turn_approval.py`](../redis_sre_agent/core/turn_approval.py) | 审批状态、checkpoint 校验、临时实例恢复和继续执行 | 审批对应同一操作/任务；恢复时重新检查访问权 |

阅读主线时可以把各阶段先当作完整步骤。修改目标选择时进入 targeting；修改最终消息保存时进入 completion。无需从所有模块的底层函数开始读。

## 为什么还有多个入口

| 用户入口 | 实际执行路径 |
| --- | --- |
| HTTP `POST /api/v1/tasks` | [`api/tasks.py`](../redis_sre_agent/api/tasks.py) → Docket `process_agent_turn` → `run_agent_turn` |
| MCP 深入诊断 | [`mcp_server/server.py`](../redis_sre_agent/mcp_server/server.py) → Docket `process_agent_turn` |
| MCP 普通/数据库问答 | 同一 MCP 模块 → `process_chat_turn` |
| 兼容知识任务 | `process_knowledge_query` → ChatAgent |
| CLI `query` | [`cli/query.py`](../redis_sre_agent/cli/query.py) → 直接调用 agent |

兼容 chat 任务的 `response` 是序列化 `AgentResponse`；路由任务的 `response` 是文本，并把部分信息放在顶层字段中。本轮复用了保存逻辑，同时保留这些返回差异。调用方因此无需同时迁移。

`knowledge` 仍是兼容模式：生产问答使用 ChatAgent，后台执行额外保留知识模式预算和可选语义缓存。独立 [`KnowledgeOnlyAgent`](../redis_sre_agent/agent/knowledge_agent.py) 仍被评测代码使用。

## 哪些检查有实际作用

- 用户选择新目标时清理旧实例、集群和 handle，防止后续工具仍访问旧目标。
- 显式目标、自然语言目标和审批恢复分别有访问检查，因为目标来源和权限变化时间不同。
- 完成前检查取消/待审批状态，并通过 `TaskManager.complete_task_if_open` 原子完成，避免并发取消被覆盖。
- 审批 ID、interrupt、graph/checkpoint 一致性和有效期检查，防止恢复错误的操作。
- 技能要求的工具调用、输出约束和模型/工具循环限制，影响回答是否遵守工作流。
- 引用入库、部分进度写入等辅助工作允许失败后继续；这些异常处理与“执行失败”的处理目的不同。

判断能否删除一个方法，不能只看是否有人显式调用。provider 可由配置路径动态加载；例如知识查询的 `_RawTextQuery._build_query_string` 是父类回调；评测专用 agent 也有独立入口。本轮没有据此删除这些代码。

## 可以按需再读的部分

知识采集/入库 `pipelines/`、评测控制 `evaluation/`、认证授权、记忆服务、语义缓存、调度、MCP 连接管理和 `monitoring/` 都保留。它们不是第一次理解模型工具循环的前置知识，但在对应功能中有实际用途。

## 改完怎么验证

已有虚拟环境时执行 `python -m pytest`；Windows 下可将 `python` 替换为 `.\.venv\Scripts\python.exe`。使用 uv 时前置 `uv run --group dev`。

| 修改范围 | 优先运行 |
| --- | --- |
| 单轮编排、取消、目标切换 | `python -m pytest tests/core/test_agent_turn_execution.py -q` |
| agent 包装、多目标隔离 | `python -m pytest tests/core/test_agent_execution.py -q` |
| 旧任务返回格式、注册名称 | `python -m pytest tests/core/test_legacy_turn_contracts.py -q` |
| 审批恢复、权限撤销、checkpoint | `python -m pytest tests/core/test_approval_resume_execution.py -q` |
| chat 工具循环、技能约束 | `python -m pytest tests/agent/test_chat_workflow.py tests/agent/test_skill_contracts.py -q` |
| 整体回归与入口导入 | `python -m pytest -q` |

这些测试把模型和存储等外部依赖换成离线替身，检查编排结果、状态转换和工具循环。真实 Redis 持久化、外部 MCP 与模型行为仍需对应集成环境或 `evals/` 的实时评测。

## 出问题从哪里定位

1. 用 `task_id` 查任务状态和进度；用 `thread_id` 找会话。API 状态查询和 CLI `task` 的实现可作参考。
2. 若在 `task_start` 后失败，先查目标准备、路由及授权；若到了 `agent_processing`，继续看 agent、模型调用和工具执行记录。
3. 回答携带 `message_id`，对应的工具证据由 `ThreadManager.set_message_trace` 保存；启用 tracing 时还可关联 `otel_trace_id`。
4. 卡在 `awaiting_approval` 时，查 `core/turn_approval.py`、审批记录和 checkpoint；不要仅重跑整个用户请求。
5. 若任务显示完成但回答文字是 `Error processing query: ...`，先看 `ChatAgent.process_query` 的异常包装。这是已识别但本轮未改变的行为问题。

审阅还确认了一个原有参数问题：triage 审批恢复会向 `get_sre_agent` 传入 `redis_instance` / `redis_cluster`，而实际 `SRELangGraphAgent` 构造函数只接收 `progress_emitter`。进入这一分支会触发参数错误，需要单独修复。本轮审批测试替换了 agent 工厂，验证的是恢复编排和结果存储，不覆盖真实构造及 checkpoint 服务。

本轮改善的是主流程的可读性和修改边界。`langgraph_agent.py`、工具管理器以及恢复分支内部仍有较大的函数；后续可分别整理，但应先为各自的行为增加验证，避免一次改变过多语义。

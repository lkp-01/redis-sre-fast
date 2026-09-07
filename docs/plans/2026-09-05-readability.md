# Readability refactor

**Goal:** Make the existing Redis SRE agent easier to follow without removing product capabilities.

**Architecture:** Keep public CLI/API/MCP names and Docket task registration stable. Separate turn orchestration, target preparation, completion and approval recovery by responsibility. Reuse `TurnScope`, `AgentResponse`, `TaskManager` and the existing approval-aware tool boundary.

**Tech stack:** Python, LangGraph, Docket, Redis, pytest, Ruff.

## Work sequence

1. Record the baseline and protect pre-existing `.gitignore` and retrieval-evaluation changes. Add offline behavioral tests for successful completion, cancellation, errors, approval interruption, routing and target changes before moving runtime code.
2. Remove the reviewed unreferenced reduction/enrichment modules and seven unused definitions. Retain framework callbacks, dynamically configured providers, and the evaluation-only knowledge agent.
3. Move skill-contract checking out of `agent/chat_agent.py`. Remove unused triage wrapper state and use the supplied agent instead of constructing a second one.
4. Extract turn completion and approval recovery from `core/docket_tasks.py`. Keep task entrypoints at their original import paths. Consolidate the legacy chat/knowledge result-persistence path while preserving their response formats and execution options.
5. Express the routed turn as named stages: prepare scope, route/authorize/discover, prepare conversation, execute, persist. Keep target precedence, authorization, fan-out, checkpoint identities and cancellation/approval handling intact.
6. Add a Chinese README and code-reading guide with actual entrypoints, responsibilities, optional subsystems and verification commands.
7. Run the complete offline test suite, focused checks on touched Python files, import/CLI smoke checks, and inspect the final diff and protected-file hashes.

## Acceptance

- No CLI command, HTTP/MCP interface, registered Docket task or product feature is removed.
- Critical offline turn tests pass before and after extraction; agent tool-loop and skill-contract behavior remain covered.
- Completion formats remain compatible, including legacy entrypoints. The previously identified error-text-as-success behavior is documented as a separate behavioral fix, not silently changed by this refactor.
- The main turn function shows execution order directly; details live in a small number of cohesive modules, not a new inheritance framework.
- User edits present before this refactor remain untouched.

## Baseline

- Existing suite: 93 tests.
- `core/docket_tasks.py`: 3,634 lines; `_process_agent_turn_impl`: 909 lines.
- `agent/chat_agent.py`: 1,472 lines.
- Candidate cleanup: two unreferenced modules (439 lines) and seven definitions (114 lines).

## 实施结果

- 后台主流程移到 `core/turn_runner.py`，按目标准备、路由、执行、保存及中断处理展开。原来的 `_process_agent_turn_impl` 导入路径保留为别名。
- 目标选择、agent 调用、多目标执行、完成保存和审批恢复分别归到 `turn_targeting.py`、`agent_execution.py`、`turn_fanout.py`、`turn_completion.py`、`turn_approval.py`。继续使用现有模型；仅增加单轮内部传递用的 `PreparedTarget`。
- 提取 `agent/skill_contracts.py`；删除两份无引用模块和七个未使用定义。保留动态 provider、框架回调和评测使用的 KnowledgeOnlyAgent。
- 归并兼容 chat/knowledge 任务的结果保存与证据写入。移除对已确定为 AgentResponse 的多余类型探测，以及同一条执行路径上的重复目标互斥检查。
- `run_agent_with_progress` 使用传入的 agent，删除废弃状态和重复构造；每个 fan-out 子任务仍单独构造 agent。
- 根 trace 使用上下文管理器，成功/异常返回后关闭 span 并恢复父上下文；新增回归测试验证这一生命周期。
- 新增中文 README 和阅读指南，说明入口差异、模块职责、验证方法和问题定位路径。
- 质量规则中的 prompt 例外仍只允许原句所在行；随代码迁移将 chat 的行号从 303 更新为 86，未放宽检查范围。

### 规模变化

| 项目 | 修改前 | 修改后 |
| --- | ---: | ---: |
| `core/docket_tasks.py` | 3,634 行 | 872 行 |
| 单轮执行主函数 | 909 行 | 160 行 |
| `agent/chat_agent.py` | 1,472 行 | 1,255 行 |
| 离线测试用例 | 93 | 122 |

将新抽出的模块计算在内，运行时代码净减少 433 行（含空行和注释，不含测试、文档和用户已有修改）。主要收益是阅读路径和修改边界变得明确；大文件缩短不等于删除了同等数量的逻辑。

### 验证结果

- 全量 `python -m pytest -q`：**122 passed**，约 9.5 秒。
- 29 个新增用例覆盖主流程、chat 工具循环、技能约束、兼容任务、并发目标隔离、审批恢复编排、取消、权限撤销、checkpoint 不一致和 trace 生命周期。
- 已修改 Python 文件的 Ruff 检查通过；全量测试包含 API/MCP 导入、CLI 帮助和仓库质量检查。
- 对比 AST，9 个 Docket 注册任务的名称和参数签名保持一致。
- 对用户已有的 `.gitignore`、CLI eval、retrieval eval 及对应测试逐一比较 SHA-256，内容保持一致。

### 未在本轮处理的行为问题

- ChatAgent 将部分执行异常包装为正常 AgentResponse，外层任务可能标为完成。
- triage 审批恢复向 `get_sre_agent` 传入实际构造函数不接受的目标参数；这一调用在整理前已经存在。

审批恢复回归测试替换了 agent 工厂与存储，验证的是编排、校验与结果保存。未运行真实 LLM、Redis、MCP 或 checkpoint 服务联调；不把离线回归通过视为这些已知问题已修复。

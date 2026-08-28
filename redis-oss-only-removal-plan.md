# Redis OSS 单例与 Cluster 专用诊断 Agent 实施计划

> **执行要求：** 使用 `executing-plans` 技能按任务顺序实施。每个任务必须先补失败测试，再做最小实现并验证；数据预检、迁移和发布门禁不得跳过。

**目标：** 将项目收敛为只诊断标准 Redis 单例和 Redis Cluster 协议端点的 Agent，完整移除 Redis Cloud、Redis Enterprise 和支持包分析逻辑，同时尽量保持通用记忆、Prompt 框架、工具框架、审批、会话、任务和知识检索能力不变。

**架构：** 以带有 `connection_url` 的 `RedisInstance` 作为唯一可诊断目标；`RedisCluster` 只作为可选的分组和拓扑元数据，不能脱离已关联的 Instance 独立绑定诊断工具。实例类型通过 Redis 协议探测确定，不再通过名称、端口、域名或 LLM 猜测产品类型。改造采用边界内硬删除，不保留 Cloud、Enterprise 或支持包运行时兼容分支。

**技术栈：** Python 3.12、FastAPI、LangGraph、redis-py、RedisVL、Click、MCP、Pytest、Ruff、uv、Prometheus/Grafana。

---

## 1. 不可变架构契约

实施前必须接受并以测试固定以下契约。

### 1.1 唯一诊断目标

- `RedisInstance` 是唯一能够连接、绑定 Provider 和执行诊断工具的目标。
- `oss_single` 表示标准 Redis standalone、主从或 Sentinel 后端中的一个直接 Redis Endpoint；本项目不增加 Sentinel 管理平面。
- `oss_cluster` 表示可通过标准 Redis 协议访问的 Redis Cluster seed endpoint。
- `RedisCluster` 只保存名称、环境、说明及 Instance 关联关系，不保存独立连接地址或管理平面凭据。
- Cluster 页面、API 或目标发现若需要启动诊断，必须解析到一个明确的已关联 `oss_cluster` Instance；不存在可用 Instance 时返回明确的“没有可诊断端点”，不能加载空 ToolManager。
- 不增加 Redis Cloud API、Redis Enterprise Admin API、CRDB、BDB、`rladmin` 或支持包的替代实现。

### 1.2 类型与 `unknown` 生命周期

- `RedisInstanceType` 只保留 `oss_single`、`oss_cluster`、`unknown`。
- `RedisClusterType` 只保留 `oss_cluster`；如果现有草稿流程必须使用 `unknown`，它只能存在于未持久化草稿模型中。
- `unknown` 只允许存在于尚未完成连接探测的临时对象、当前请求或恢复快照中。
- `save_instances()`、正式 API/CLI/MCP 创建入口和 Target Catalog 必须拒绝持久化 `unknown`。
- 新建或修改 `connection_url` 时执行协议探测：优先读取 `INFO` 中的 `cluster_enabled`，必要时使用 `CLUSTER INFO` 交叉确认。
- 探测成功后持久化为 `oss_single` 或 `oss_cluster`；连接失败、权限不足或结果不确定时不创建正式目标，并返回可操作的错误。
- 显式传入的类型与协议探测结果不一致时拒绝保存，不能静默覆盖。
- 删除基于名称、描述、端口、域名或 LLM 的类型推断和自动写回。

### 1.3 Redis Cluster 能力边界

- 保留标准 Redis 只读诊断：`INFO`、`CLUSTER INFO`、`CLUSTER NODES`、`CLUSTER SLOTS`、复制状态、内存、客户端、慢日志和延迟信息。
- “failover 诊断”仅表示根据节点 flags、slot、复制链路和 cluster state 判断故障与切换准备度。
- 不执行 `CLUSTER FAILOVER`、`CLUSTER RESET`、`CLUSTER MEET`、`CLUSTER FORGET`、slot 迁移或其他写操作。
- 当前仍使用 seed-node `Redis.from_url()`；本阶段不扩展为集群范围 key routing 或跨节点扫描。如果未来需要该能力，单独设计 `RedisCluster` 客户端方案。

### 1.4 Managed Endpoint 的处理方式

- 如果 Redis Cloud、Redis Enterprise 或其他托管服务暴露兼容的 Redis Endpoint，只按探测到的标准 Redis standalone/cluster 协议诊断。
- 不识别其产品身份，不读取管理 API，不给出平台专用操作，不承诺平台内部拓扑可见。
- Prompt 只保留一句产品中立的能力边界，不继续逐项介绍已删除产品。

### 1.5 明确保留和不做的事项

保持不变：

- Agent Memory 的通用实现和用户记忆结构。
- LangGraph 的线程、任务、审批、恢复和调度框架。
- 通用 ToolProvider、ToolManager、缓存、审计和调用路由抽象。
- 通用 Prometheus、日志、知识检索和 GitHub 等非目标产品 Provider。
- API、CLI 和 MCP 的总体架构及未被删除的 URL/响应结构。

本计划不做：

- 不借机重构无关模块。
- 不增加 Redis Sentinel 管理能力。
- 不增加会修改 Redis 状态的 Cluster 管理工具。
- 当前仓库不存在 `ui/` 目录，因此不在本计划中修改前端；如果 UI 位于其他仓库，必须另建对应计划和发布门禁。

## 2. 当前基线与实施门禁

当前已确认的基线事实：

- 仓库没有 `tests/` 目录，但 `pyproject.toml` 将其配置为测试目录。
- `README.md` 和 `LICENSE.txt` 不存在，导致所有 `uv run` 和 editable build 在执行命令前失败。
- 当前环境中的 MCP 2.x 与代码使用的 MCP 1.x `FastMCP` API 不兼容。
- 直接使用现有虚拟环境运行 `python -m compileall -q redis_sre_agent` 可以通过。
- CLI help 可以启动，并仍公开 `support-package` 命令。
- `ui/src/...` 不存在，原计划中的前端阶段不属于本仓库。

发布门禁：

1. 未建立可重复的构建和测试基线前，不开始产品逻辑删除。
2. 未通过旧数据 raw preflight 前，不收窄 Enum 或删除兼容字段。
3. 未完成任务/恢复状态排空前，不部署不兼容模型。
4. 未通过真实 standalone 与 Redis Cluster 冒烟测试前，不发布。
5. 未通过运行时代码残留扫描、构建产物检查和依赖树检查前，不视为完成。

## Task 0：修复并记录可重复基线

**文件：**

- 修改：`pyproject.toml`
- 修改：`uv.lock`
- 恢复或补齐：`README.md`
- 恢复：`LICENSE.txt`
- 新建：`tests/smoke/test_imports.py`
- 新建：`tests/smoke/test_cli_help.py`

**步骤：**

1. 从项目权威来源或版本历史恢复 `README.md` 和 `LICENSE.txt`；不得自行编造许可证文本。如果找不到权威许可证，停止实施并由项目所有者决定是否移除 `license` 元数据。
2. 将 MCP 依赖暂时约束为与当前代码兼容的 `mcp>=1.23.3,<2`；迁移 MCP 2.x 不属于本次裁剪范围。
3. 新增导入 smoke test，至少导入 API app、ToolManager、target registry 和 MCP server。
4. 新增 CLI smoke test，断言主帮助可以执行，并记录当前仍存在的 `support-package` 命令作为待删除基线。
5. 安装开发依赖并运行基线：

```powershell
uv lock
uv sync --group dev
uv run python -m compileall -q redis_sre_agent
uv run ruff check redis_sre_agent tests
uv run pytest tests/smoke -v
uv run redis-sre-agent --help
uv build
```

6. 将所有与本改造无关且暂时无法修复的失败写入变更记录；后续验收不得新增失败。

**预期：** 构建、导入和 smoke test 可重复执行；基线故障与本次改造故障能够区分。

**建议提交：** `test: establish executable OSS-only migration baseline`

## Task 1：用测试固定目标和类型契约

**文件：**

- 新建：`tests/core/test_supported_target_types.py`
- 新建：`tests/core/test_instance_persistence_contract.py`
- 新建：`tests/core/test_cluster_target_contract.py`
- 修改：`redis_sre_agent/core/instances.py`
- 修改：`redis_sre_agent/core/clusters.py`

**步骤：**

1. 先写失败测试，覆盖：
   - Instance 的正式持久化只接受 `oss_single`、`oss_cluster`。
   - `unknown` 可用于临时对象，但 `save_instances()` 必须拒绝。
   - Cluster 正式记录只接受 `oss_cluster`。
   - RedisCluster 没有 Admin API 凭据，也不被视为独立诊断 Endpoint。
   - Cloud、Enterprise 和任意未知产品类型产生明确验证错误。
2. 运行测试并确认它们在当前实现下失败。
3. 只实现类型契约和持久化防线；此任务暂不删除旧字段，以便数据迁移工具仍可在旧模型上运行。
4. 运行测试，确认契约通过。

```powershell
uv run pytest tests/core/test_supported_target_types.py tests/core/test_instance_persistence_contract.py tests/core/test_cluster_target_contract.py -v
```

**建议提交：** `test: define Redis OSS diagnostic target contract`

## Task 2：实现确定性的 Redis 协议类型探测

**文件：**

- 新建：`redis_sre_agent/core/redis_topology.py`
- 新建：`tests/core/test_redis_topology.py`
- 修改：`redis_sre_agent/agent/langgraph_agent.py`
- 修改：`redis_sre_agent/core/docket_tasks.py`
- 修改：`redis_sre_agent/core/instances.py`
- 修改：`redis_sre_agent/api/instances.py`
- 修改：`redis_sre_agent/cli/instance.py`
- 修改：`redis_sre_agent/mcp_server/server.py`

**步骤：**

1. 为独立探测函数写失败测试，使用 mock Redis client 覆盖：
   - `cluster_enabled=0` 返回 `oss_single`。
   - `cluster_enabled=1` 返回 `oss_cluster`。
   - `INFO` 字段缺失时用 `CLUSTER INFO` 交叉确认。
   - 权限不足、超时、TLS 错误和协议不确定时返回结构化失败，不返回猜测类型。
2. 实现 `probe_redis_topology(connection_url)`，结果包含类型、探测证据和安全错误摘要；日志不得输出连接密码。
3. 删除 `_detect_instance_type_with_llm()` 及其调用、memoize key 和自动写回逻辑。
4. API、CLI、MCP 创建和修改连接地址时统一调用探测函数，不各自复制判断逻辑。
5. 动态会话目标可在连接前短暂为 `unknown`；连接成功后必须先分类再写入 session/resume snapshot。
6. 添加显式类型与探测结果不一致的失败测试。

```powershell
uv run pytest tests/core/test_redis_topology.py tests/api/test_instances_oss_only.py tests/cli/test_instance_oss_only.py tests/mcp/test_instances_oss_only.py -v
```

**预期：** 代码中不再存在基于 LLM、端口、hostname、名称或描述的产品类型检测。

**建议提交：** `feat: classify Redis topology through protocol probing`

## Task 3：补齐只读 Redis Cluster 诊断能力

**文件：**

- 修改：`redis_sre_agent/tools/diagnostics/redis_command/provider.py`
- 修改：`redis_sre_agent/tools/manager.py`
- 新建：`tests/tools/diagnostics/test_redis_cluster_tools.py`
- 新建：`tests/tools/test_tool_manager_oss_only.py`

**步骤：**

1. 写失败测试，要求 `oss_cluster` Instance 暴露 `cluster_info`、`cluster_nodes`、`cluster_slots` 和 `replication_info`。
2. 测试 standalone Instance 不因 Cluster 命令不支持而导致整个 Provider 初始化失败。
3. 实现 `CLUSTER NODES` 和 `CLUSTER SLOTS` 的只读工具及稳定的结构化输出。
4. 复用节点 flags、slot 和 replication 信息形成 failover readiness 数据，不增加 failover 执行工具。
5. 删除 “OSS Cluster cluster-specific tools not yet implemented” 分支，改为加载通用 Redis Provider。
6. 添加安全测试，确保不存在 `CLUSTER FAILOVER`、`RESET`、`MEET`、`FORGET`、`SETSLOT` 等执行路径。

```powershell
uv run pytest tests/tools/diagnostics/test_redis_cluster_tools.py tests/tools/test_tool_manager_oss_only.py -v
```

**建议提交：** `feat: add read-only Redis Cluster topology diagnostics`

## Task 4：收敛 Target Binding 与 Target Catalog

**文件：**

- 修改：`redis_sre_agent/targets/redis_binding.py`
- 修改：`redis_sre_agent/targets/registry.py`
- 修改：`redis_sre_agent/core/config.py`
- 修改：`redis_sre_agent/core/targets.py`
- 修改：`redis_sre_agent/core/redis.py`
- 新建：`tests/targets/test_redis_binding_oss_only.py`
- 新建：`tests/core/test_target_catalog_oss_only.py`

**步骤：**

1. 写失败测试，固定以下行为：
   - `redis.data` 只为 Instance 创建客户端。
   - `oss_cluster` Instance 能加载通用 Redis Provider。
   - Cluster 元数据记录本身不能加载诊断 Provider。
   - Cluster 可展示关联 Instance；无关联 Endpoint 时返回不可诊断状态。
   - Catalog 中不包含 Cloud/Admin 字段或能力。
2. 从默认 target integration registry 删除 `redis.enterprise_admin` 和 `redis.cloud` factory。
3. 删除 Enterprise/Cloud BindingStrategy 分支和 provider load request。
4. 删除 `_TYPE_HINTS`、capabilities、search aliases、schema 和 HSET mapping 中的平台字段。
5. Catalog 重建时删除旧 `sre_targets:*` hashes 后从 Instance/Cluster 权威记录重建，避免旧 HSET 字段残留；此行为只允许在迁移/维护命令中执行。

```powershell
uv run pytest tests/targets/test_redis_binding_oss_only.py tests/core/test_target_catalog_oss_only.py -v
```

**建议提交：** `refactor: make Redis instances the only diagnostic targets`

## Task 5：编写部署前 raw 数据预检和迁移工具

该任务必须在删除旧 Enum 和字段之前完成，并用旧版本模型验证。迁移工具直接读取 Redis 原始值，不能依赖可能跳过非法记录的 Pydantic loader。

**文件：**

- 新建：`scripts/migrations/redis_oss_only_preflight.py`
- 新建：`scripts/migrations/redis_oss_only_apply.py`
- 新建：`tests/migrations/test_redis_oss_only_preflight.py`
- 新建：`tests/migrations/test_redis_oss_only_apply.py`
- 修改：`redis_sre_agent/core/keys.py`（仅在需要集中暴露已有 key pattern 时）

**预检范围：**

- Instance 和 Cluster 原始 JSON/hashes。
- `sre_targets:*` Target Catalog hashes。
- thread-scoped instances。
- approval resume state 和 `staged_session_instance`。
- queued/running tasks、schedules 和缓存中引用的旧 target/provider/tool 名称。
- Agent Memory 中与旧平台资产明确绑定的记录；不扫描或删除无关用户记忆。
- 本地支持包目录与配置的 S3 prefix；报告中不得输出包内容或凭据。

**步骤：**

1. 为 dry-run 写测试，确保报告列出 unsupported records、遗留字段、运行中任务和支持包产物数量，但不修改数据。
2. 报告不得解密或打印 Redis URL 密码、Admin 密码、Cloud API key 等秘密。
3. `apply` 模式必须要求显式备份路径和确认参数；默认只做 dry-run。
4. 不自动把 `redis_enterprise` 映射成 `oss_cluster`，也不把 `redis_cloud` 映射成 `oss_single`。
5. 每条旧产品记录只允许备份后删除，或由用户提供 Redis Endpoint 后重新协议探测并登记为新的 OSS Instance。
6. 对仍保留的 OSS 记录清除 `admin_*`、`redis_cloud_*` 和扩展 secret 中的平台凭据。
7. 在部署窗口排空或取消引用旧 provider/target 的任务、审批和恢复状态。
8. 支持包文件/S3 对象先生成清单，由数据所有者决定保留期；只有显式批准后才能删除。
9. 迁移结束后重建 Target Catalog，并再次运行 dry-run；结果必须为零阻断项。

```powershell
uv run pytest tests/migrations -v
uv run python scripts/migrations/redis_oss_only_preflight.py --output artifacts/oss-only-preflight.json
# 仅在已备份并审核报告后执行：
uv run python scripts/migrations/redis_oss_only_apply.py --report artifacts/oss-only-preflight.json --backup <approved-backup-path> --confirm
```

**发布门禁：** preflight 仍发现旧类型、遗留秘密、运行中旧任务或未决支持包数据时，禁止继续 Task 6。

**建议提交：** `feat: add OSS-only data migration preflight`

## Task 6：收窄领域模型并删除产品专用字段

**文件：**

- 修改：`redis_sre_agent/core/instances.py`
- 修改：`redis_sre_agent/core/clusters.py`
- 修改：`redis_sre_agent/core/instance_mutation_helpers.py`
- 修改：`redis_sre_agent/core/instance_inspection_helpers.py`
- 修改：`redis_sre_agent/core/cluster_helpers.py`
- 修改：`redis_sre_agent/core/docket_tasks.py`
- 修改：`redis_sre_agent/core/targets.py`
- 修改：`redis_sre_agent/core/redis.py`
- 删除：`redis_sre_agent/core/cluster_admin_defaults.py`

**步骤：**

1. 删除 `redis_enterprise`、`redis_cloud` Enum 值。
2. 删除 Instance 中的 `admin_url`、`admin_username`、`admin_password`、`redis_cloud_*` 字段和 `get_bdb_uid()`。
3. 删除 Cluster Admin API 凭据字段、序列化、解密和验证。
4. 删除 mutation/inspection/docket payload 中的平台字段及 secret 处理。
5. 保留 `cluster_id`、Redis connection URL、TLS、Redis ACL username/password 和 extension 通用机制。
6. 对持久化加载错误改为可观察、可计数的失败；不得在数据迁移后仍静默跳过未知产品类型。

```powershell
uv run pytest tests/core tests/migrations -v
```

**建议提交：** `refactor: remove managed Redis fields from domain models`

## Task 7：删除 Cloud、Enterprise 和支持包模块及配置

**删除：**

- `redis_sre_agent/tools/cloud/redis_cloud/`
- `redis_sre_agent/tools/admin/redis_enterprise/`
- `redis_sre_agent/tools/support_package/`
- `redis_sre_agent/api/support_package.py`
- `redis_sre_agent/cli/support_package.py`
- `redis_sre_agent/core/support_package_helpers.py`
- `redis_sre_agent/pipelines/scraper/redis_cloud_api.py`

**修改：**

- `redis_sre_agent/core/config.py`
- `redis_sre_agent/tools/manager.py`
- `redis_sre_agent/targets/redis_binding.py`
- `redis_sre_agent/api/app.py`
- `redis_sre_agent/cli/main.py`
- `redis_sre_agent/pipelines/orchestrator.py`
- `redis_sre_agent/cli/pipeline.py`

**步骤：**

1. 在删除目录前写导入测试，枚举运行时 registry/provider path，确保删除后不会尝试动态导入旧类。
2. 删除 default registry 中的旧 client factories。
3. 删除 ToolManager 中的 Enterprise Admin、Cloud 和支持包参数、构造器、动态 import、provider alias 和 close 分支。
4. 删除支持包本地/S3配置字段及环境变量读取。
5. 删除 API router、CLI command 和 pipeline scraper 注册。
6. 删除空的 `tools/cloud/`、`tools/admin/` 包。

```powershell
uv run pytest tests/smoke/test_imports.py tests/tools/test_tool_manager_oss_only.py tests/targets/test_redis_binding_oss_only.py -v
uv run python -m compileall -q redis_sre_agent
```

**建议提交：** `refactor: delete managed Redis and support package providers`

## Task 8：移除支持包数据流并保留通用会话机制

**文件：**

- 修改：`redis_sre_agent/core/turn_scope.py`
- 修改：`redis_sre_agent/core/query_helpers.py`
- 修改：`redis_sre_agent/core/approvals.py`
- 修改：`redis_sre_agent/core/docket_tasks.py`
- 修改：`redis_sre_agent/agent/router.py`
- 修改：`redis_sre_agent/agent/chat_agent.py`
- 修改：`redis_sre_agent/agent/langgraph_agent.py`
- 修改：`redis_sre_agent/cli/query.py`
- 修改：`redis_sre_agent/mcp_server/server.py`
- 修改：`redis_sre_agent/core/cli_mcp_parity.py`
- 新建：`tests/core/test_turn_scope_oss_only.py`
- 新建：`tests/agent/test_router_oss_only.py`

**步骤：**

1. 写失败测试，确认 TurnScope 只保留 zero-scope 和 target-bindings。
2. 删除 `support_package_id`、`support_package_path`、`support_package_context` 和 `scope_kind="support_package"`。
3. 删除支持包优先路由、Agent context 注入、ToolManager 参数、CLI/MCP 查询参数和 parity 映射。
4. 保留线程、任务、审批和通用 resume state；只删除其中支持包字段。
5. 测试旧支持包 resume state 已在迁移阶段被拒绝或清理，而不是在运行时兼容。

```powershell
uv run pytest tests/core/test_turn_scope_oss_only.py tests/agent/test_router_oss_only.py -v
```

**建议提交：** `refactor: remove support package request flow`

## Task 9：收窄 Prompt、Agent 和安全修正规则

**文件：**

- 修改：`redis_sre_agent/agent/prompts.py`
- 修改：`redis_sre_agent/agent/langgraph_agent.py`
- 修改：`redis_sre_agent/agent/chat_agent.py`
- 修改：`redis_sre_agent/agent/router.py`
- 修改：`redis_sre_agent/agent/subgraphs/recommendation_worker.py`
- 修改：`redis_sre_agent/agent/subgraphs/safety_fact_corrector.py`
- 新建：`tests/agent/test_prompt_scope_oss_only.py`
- 新建：`tests/agent/test_recommendation_safety_oss_only.py`

**步骤：**

1. 写失败测试，确保生成 Prompt 和路由文本中不存在 Cloud、Enterprise、CRDB、BDB、`rladmin`、Admin REST API 或支持包说明。
2. 删除平台专用 INFO 解释、凭据缺失回复、URL allowlist、推荐命令格式和 product-specific safety correction。
3. 保留通用事实纠正、安全规则、知识检索、memory 注入和只读优先原则。
4. 增加唯一范围说明：

> This agent diagnoses standard Redis standalone and Redis Cluster protocol endpoints only. It does not use vendor management APIs or offline support packages.

5. Cluster 建议只能基于实际工具证据，不得假设所有节点已被 seed endpoint 完整覆盖。

```powershell
uv run pytest tests/agent/test_prompt_scope_oss_only.py tests/agent/test_recommendation_safety_oss_only.py -v
```

**建议提交：** `refactor: constrain agent reasoning to standard Redis protocols`

## Task 10：收窄 API、CLI 和 MCP 外部入口

**文件：**

- 修改：`redis_sre_agent/api/instances.py`
- 修改：`redis_sre_agent/api/clusters.py`
- 修改：`redis_sre_agent/api/app.py`
- 修改：`redis_sre_agent/cli/instance.py`
- 修改：`redis_sre_agent/cli/cluster.py`
- 修改：`redis_sre_agent/cli/main.py`
- 修改：`redis_sre_agent/mcp_server/server.py`
- 修改：`redis_sre_agent/mcp_server/__init__.py`
- 修改：`redis_sre_agent/core/cli_mcp_parity.py`
- 修改或删除：`redis_sre_agent/core/migrations/instances_to_clusters.py`
- 新建：`tests/api/test_instances_oss_only.py`
- 新建：`tests/api/test_clusters_oss_only.py`
- 新建：`tests/cli/test_oss_only_commands.py`
- 新建：`tests/mcp/test_oss_only_tools.py`

**步骤：**

1. 写 API/CLI/MCP 对等测试，覆盖相同的类型白名单、协议探测和错误语义。
2. 删除所有 Admin、Cloud 和支持包请求/响应字段及 endpoint/tool/command。
3. Instance 创建接口增加可选显式 `instance_type`，但最终类型必须经协议探测确认。
4. Cluster 创建只创建 `oss_cluster` 分组元数据；诊断必须选择或解析一个关联的 `oss_cluster` Instance。
5. 删除 `cluster backfill-instance-links` CLI/MCP 永久入口。若迁移仍需要 OSS 关联逻辑，将其移入 Task 5 的一次性脚本，产品运行时不保留 migration tool。
6. 更新 OpenAPI、CLI help、MCP docstring 和 parity 映射。
7. 除明确删除的字段和 endpoint 外，保持其他 URL 和响应结构不变。

```powershell
uv run pytest tests/api tests/cli tests/mcp -v
uv run redis-sre-agent --help
uv run redis-sre-agent instance --help
uv run redis-sre-agent cluster --help
```

**预期：** CLI help 中不存在 `support-package`；MCP tool list 不包含旧产品或支持包工具。

**建议提交：** `refactor: expose only OSS Redis targets across API CLI and MCP`

## Task 11：清理知识摄取、评测与监控资产

**知识与 scraper 文件：**

- 修改：`redis_sre_agent/pipelines/scraper/redis_docs.py`
- 修改：`redis_sre_agent/pipelines/scraper/redis_docs_local.py`
- 修改：`redis_sre_agent/pipelines/scraper/base.py`
- 修改：`redis_sre_agent/pipelines/processor_source_helpers.py`
- 修改：`redis_sre_agent/pipelines/ingestion/document_processor.py`
- 修改：`redis_sre_agent/pipelines/orchestrator.py`
- 修改：`redis_sre_agent/cli/pipeline.py`
- 修改：`redis_sre_agent/knowledge_pack/builder.py`
- 修改：`redis_sre_agent/tools/knowledge/knowledge_base.py`

**评测与监控文件：**

- 修改：`redis_sre_agent/evaluation/runtime.py`
- 修改：`redis_sre_agent/evaluation/retrieval_eval.py`
- 修改：`redis_sre_agent/evaluation/tool_identity.py`
- 删除或改写：`evals/corpora/` 中旧平台文档、技能和工单
- 删除或改写：`evals/scenarios/` 与 `evals/goldens/` 中旧平台和支持包场景
- 删除：`monitoring/grafana/provisioning/dashboards/json/redis-enterprise-logs.json`
- 修改：`monitoring/prometheus.yml`
- 新建：`tests/pipelines/test_redis_docs_oss_allowlist.py`
- 新建：`tests/evaluation/test_oss_only_corpus.py`

**步骤：**

1. 将文档抓取从广泛的 `operate/` 递归改成明确的 OSS allowlist。
2. 明确排除 `operate/rs/`、`operate/rc/`、`stack-with-enterprise/`、Cloud API、Admin API 和 `rladmin` 路径；链接递归也必须执行同一过滤器。
3. 删除 `DocumentCategory.ENTERPRISE` 及相关 source mapping 和 Enterprise whole-document chunking。
4. 删除 Cloud spec revision、scraper 注册和 pipeline help。
5. 删除旧知识包、源文档、索引批次和 eval corpus；随后从 allowlist 来源重建。
6. 不允许简单删除所有旧 eval 而降低覆盖率。至少用 OSS 场景替换 standalone 内存/延迟/慢查询、Cluster state、slot、replication/failover readiness、多个 OSS target 发现和消歧。
7. Golden 结果必须人工审查，不能无条件批量接受。
8. 删除 Enterprise exporter job/dashboard，同时保留通用 Redis、Agent、日志和 Prometheus 监控。
9. Agent Memory 实现不改；只根据 Task 5 报告清理明确绑定旧平台资产的记忆，不清空用户记忆。

```powershell
uv run pytest tests/pipelines/test_redis_docs_oss_allowlist.py tests/evaluation/test_oss_only_corpus.py -v
uv run redis-sre-agent eval validate
```

**建议提交：** `refactor: rebuild knowledge and eval assets for OSS Redis`

## Task 12：清理依赖、构建配置和生成产物

**文件：**

- 修改：`pyproject.toml`
- 修改：`uv.lock`
- 修改：相关 Ruff、Black、Coverage 配置

**步骤：**

1. 删除 `redis-enterprise`。
2. 确认 `boto3` 只由支持包 S3 storage 使用后删除。
3. 确认 `aiofiles` 无其他调用后删除。
4. 删除只用于 Redis Cloud 生成客户端的 `openapi-python-client`。
5. 删除已不存在生成客户端目录的 Ruff、Black、Coverage 排除项。
6. 保留 Task 0 中的 MCP 1.x 上界，除非另一个独立改造已完成 MCP 2.x 迁移。
7. 更新 lock、重建 wheel，并检查 wheel 不包含已删除模块、平台文档或支持包代码。

```powershell
uv lock
uv sync --group dev
uv tree
uv build
uv run python -c "import zipfile, pathlib; p=next(pathlib.Path('dist').glob('*.whl')); names=zipfile.ZipFile(p).namelist(); forbidden=[n for n in names if any(x in n.lower() for x in ('redis_cloud','redis_enterprise','support_package'))]; assert not forbidden, forbidden"
```

**建议提交：** `build: remove managed Redis and support package dependencies`

## Task 13：建立自动残留扫描门禁

简单 `rg` 不能同时处理迁移工具、负向测试和变更文档中的合法历史字符串，因此使用显式 allowlist 检查器。

**文件：**

- 新建：`scripts/quality/check_oss_only_residuals.py`
- 新建：`scripts/quality/oss_only_residual_allowlist.txt`
- 新建：`tests/quality/test_oss_only_residuals.py`

**扫描标识至少包括：**

- `redis cloud`、`redis_cloud`、`redis.cloud`
- `redis enterprise`、`redis_enterprise`、`redis.enterprise_admin`
- `support package`、`support_package`
- `rladmin`、`re_admin`、`crdb`、BDB 专用字段
- `admin_url`、`admin_username`、`admin_password`
- `redis_cloud_*`
- Cloud/Enterprise 管理凭据环境变量
- `DocumentCategory.ENTERPRISE`
- `redislabs.com`

**允许命中范围：**

- 本实施计划。
- Task 5 的一次性迁移脚本及其测试。
- 明确的发布说明或历史变更记录。
- 负向测试 fixture。

运行时代码、Prompt、知识源、eval、监控、构建配置和 wheel 中不得命中。

```powershell
uv run python scripts/quality/check_oss_only_residuals.py
uv run pytest tests/quality/test_oss_only_residuals.py -v
```

**建议提交：** `test: enforce OSS-only runtime boundary`

## Task 14：端到端验证与发布

**自动验证：**

```powershell
uv sync --group dev
uv run ruff check redis_sre_agent tests scripts
uv run python -m compileall -q redis_sre_agent
uv run pytest
uv run python -c "import redis_sre_agent.api.app; import redis_sre_agent.mcp_server.server; import redis_sre_agent.tools.manager"
uv run redis-sre-agent --help
uv build
uv run python scripts/quality/check_oss_only_residuals.py
```

**真实 Redis 冒烟测试：**

1. 启动一个标准 Redis standalone。
2. 启动至少三节点 Redis Cluster，确保 slots 已分配。
3. 登记 standalone endpoint，验证探测结果为 `oss_single`。
4. 登记 Cluster seed endpoint，验证探测结果为 `oss_cluster`。
5. 验证 standalone 的 INFO、SLOWLOG、CLIENT、MEMORY、LATENCY、replication 工具。
6. 验证 Cluster 的 `CLUSTER INFO`、`CLUSTER NODES`、`CLUSTER SLOTS`、replication 和 failover readiness 输出。
7. 验证 Cluster 元数据记录没有关联 Instance 时不能启动诊断。
8. 验证连接失败、权限不足、错误显式类型和 `unknown` 持久化均返回明确错误。
9. 验证工具列表没有 Cloud、Enterprise、Admin API 和支持包工具。
10. 验证通用记忆、线程、审批、任务恢复、调度、知识检索和 Prometheus Provider 正常工作。

**数据发布顺序：**

1. 停止接受新任务。
2. 排空或取消旧版本运行中任务和未决审批。
3. 使用旧版本执行最终 raw preflight 和备份。
4. 执行已批准的数据清理/重新登记。
5. 部署 OSS-only 代码和 lockfile。
6. 重建 Instance、Cluster、Target、知识和 eval 相关索引。
7. 执行 standalone/Cluster 冒烟测试。
8. 观察错误率、目标加载失败、工具调用失败和旧 key 命中情况。
9. 验证通过后恢复流量。

**回滚要求：**

- 保留迁移前数据备份和旧版本构建产物。
- 回滚只恢复代码和原始数据备份，不把已删除产品记录自动映射成 OSS 类型。
- 支持包文件的删除按单独的数据保留审批处理，不能依赖应用回滚恢复。

## 3. 最终完成标准

- 正式持久化的 Instance 只能是 `oss_single` 或 `oss_cluster`。
- Instance 是唯一诊断 Endpoint；Cluster 记录仅用于分组和关联。
- 类型由 Redis 协议确定，不再由 LLM 或元数据猜测。
- OSS Cluster 能提供只读 topology、slot、replication 和 failover readiness 诊断。
- API、CLI、MCP 不再暴露 Cloud、Enterprise、Admin API 或支持包入口。
- Python 包和 wheel 不包含三个专用 Provider、Cloud 生成客户端或支持包代码。
- 运行时注册表、配置、Prompt、知识、eval 和监控不再引用旧产品。
- 不再安装 `redis-enterprise`、Cloud client generator，以及仅供支持包使用的依赖。
- 旧数据经过 raw preflight、备份、人工分类和清理，没有被新模型静默跳过。
- Target Catalog hashes 和索引从权威 OSS 数据完整重建，没有遗留 Cloud/Admin 字段。
- 通用记忆、审批、会话、任务、调度、知识检索、监控和 Redis 诊断保持工作。
- standalone 和真实三节点 Redis Cluster 冒烟测试通过。
- Ruff、compileall、pytest、导入、CLI、MCP、wheel 检查和残留扫描全部通过。

## 4. 风险评级

整体属于中等规模、**中高架构与迁移风险**的收敛任务。

主要风险按优先级排列：

1. 现有 Cluster 资源没有诊断连接信息，若不先固定 Instance 端点模型，删除 Enterprise Provider 后 Cluster 目标将失去全部工具。
2. 旧 Enum 收窄后记录可能被 loader 静默跳过，因此 raw preflight 和迁移必须先于模型删除。
3. `unknown` 若继续持久化，会让“仅支持两种 OSS 类型”的边界失效。
4. 动态 Provider registry、Prompt、知识 scraper、eval 和恢复状态中的间接引用容易造成启动错误或产品逻辑回流。
5. 文档 crawler 的广泛 `operate/` 路径会重新摄取 Enterprise 内容，必须使用 allowlist。
6. 当前构建、测试和 MCP 基线不可用，若不先修复将无法判断回归来源。
7. 支持包和管理凭据属于敏感遗留数据，删除代码不等于删除存储数据，必须独立审计和审批。

只有 Task 0、Task 1、Task 2 和 Task 5 的门禁全部满足后，才允许开始不可逆的模块删除。

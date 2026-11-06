# PostgreSQL 并发协议

本次重构沿用 PostgreSQL 与 PGVector，没有新增 Redis。数据库约束、短事务、行锁及客户端版本共同保护写入；不能仅依赖 MVCC 快照、Python 进程内锁，或在旧快照上直接保存完整对象。

后续查询与索引验证见 [index-audit.md](index-audit.md)，包含真实业务库计划、规模验证及三项查询索引的并发安装方式。

## 写入边界

| 路径 | 并发控制 | 冲突与恢复 |
| --- | --- | --- |
| 营销工作流启动、审核 | 每个 Brief 的 PostgreSQL advisory lock；锁、checkpoint 写入和 Brief 投影使用同一物理连接 | 同时执行返回 409；连接失效后旧执行器不能换连接继续写入 |
| 人工审核 | 客户端提交所见稿件的 `expected_checkpoint_id` | 旧稿件不能批准新稿件；409 后重新读取并由用户重新决定 |
| Brief 工作流投影 | 校验最新 checkpoint，并用 `workflow_version` 条件更新 | 旧快照不能覆盖新结果；GET 的正文、审核报告和 token 来自同一 checkpoint |
| 文档替换、删除 | 文档行锁与 `expected_version`；正文、原始文件、元数据和向量块同一事务提交 | 并发替换只有一个旧版本请求成功；失败或取消整体回滚；删除后更新不会复活向量 |
| 评测复核、重建报告 | 先锁运行，再读取/锁定任务，合并本次 PATCH，刷新汇总后一起提交 | 不同维度修改不会互相覆盖；不同任务修改不会留下过期汇总 |
| 评测基线 | 锁用户行后切换标记；数据库部分唯一索引兜底 | 每个用户、测试集最多一个基线 |
| 评测执行 | 数据库全局租约，携带 owner 与递增 generation；每次结果写入在同一事务中验证租约 | 跨进程仍最多一个执行器；过期执行器的迟到结果被拒绝 |
| Google 身份绑定 | 条件更新仅允许未绑定或同一个 `google_sub`；唯一约束处理同时创建 | 同一身份重试幂等；两个不同身份不能覆盖同一邮箱的绑定 |

文档解析、embedding、LLM 调用都在业务行锁事务之外。营销工作流执行期间保留专用连接及 advisory lock，但不会跨模型请求持有长数据库事务。它需要直连 PostgreSQL 或具备会话亲和性的代理，不能走 PgBouncer transaction pooling。

评测租约有效期 60 秒，每 10 秒续约；启动、新建评测和读取评测时会回收过期租约。进程启动不会把其他进程仍在运行的评测标记为失败。评测列表、详情及对比中需要组合多次查询的响应使用 `REPEATABLE READ` 只读事务，避免混合旧汇总与新明细。

没有保存原始正文的旧文档，在回退读取向量文本时重新读取元数据，并让元数据与文本共用一个只读快照、一个连接，避免返回旧文件名与新正文，也避免单连接池相互等待。

评测复核仍是部分字段 PATCH：只提交实际修改的评分维度。同一维度的两次明确修改按获得运行锁的顺序生效；本次未增加评测审批历史或针对同一维度的页面版本冲突提示。

工作流 checkpoint 与 Brief 投影分步持久化。执行中断后以 checkpoint 为准恢复，不能把旧 Brief 内容当作当前稿件，也不能重复提交已经写入 checkpoint 的人工决定。模型调用本身不承诺 exactly-once；故障恢复可能重做尚未持久化的生成步骤。

## 接口变更

- `GET /api/briefs/workflow` 和启动/审核结果增加 `checkpoint_id`。
- 普通及流式审核请求必须包含 `expected_checkpoint_id`，取自用户当前看到的工作流结果。
- 文档列表、上传和替换结果增加 `version`，从 1 开始递增。
- `PUT /api/documents/{id}` 与 `DELETE /api/documents/{id}` 必须带查询参数 `expected_version`。
- 缺少必需版本返回 422；版本过期返回 409；文档已经删除返回 404。
- 流已经发出响应头后，失败通过 NDJSON `error` 事件中的 `status` 表达，客户端仍需检查流事件。

项目现有营销审核和文档删除页面已传入所见版本。冲突后刷新展示，不自动用新 token 重放旧审核/删除请求。

## 升级现有数据库

启动迁移新增 `briefs.workflow_version`、`briefs.workflow_checkpoint_id`、`knowledge_documents.version`、`eval_executor_leases` 及基线唯一索引。迁移可重复执行。历史重复基线只清除多余标记，保留所有运行；优先保留已完成且完成时间较新的记录。

多进程冷启动由专用 PostgreSQL 会话串行初始化 PGVector、业务 schema 和 checkpoint 表。等待者通过短查询尝试取锁，避免阻塞取锁的查询快照与 checkpoint 的并发索引创建互相等待。

升级时需要停止所有旧版本后端 worker，再启动新版本。旧代码没有执行所有权与版本校验，不能与新版本混合运行。此次开发仅在隔离测试库执行迁移，未修改业务数据库或重启应用。

旧版本遗留的 `queued/running` 评测没有租约，启动恢复不会猜测它们是否仍被执行。停止全部 worker、完成新 schema 迁移后，使用离线恢复脚本显式将这些记录标记失败并更新汇总；保留已完成任务与历史数据。命令和确认参数见 `scripts/recover_legacy_evaluations.py --help`。该脚本不能代替停止旧进程。

```sh
GLOBALPILOT_RECOVERY_DATABASE_URL='显式指定目标 PostgreSQL URL' \
  .venv/bin/python scripts/recover_legacy_evaluations.py --confirm-workers-stopped
```

脚本不会读取 `.env`，遇到未过期租约会拒绝恢复。

历史上已经发生的评分覆盖、混合向量或孤立向量不能由新协议还原；需要从原始资料或备份单独核对、重建。新协议保护升级后的写入。

## 回归验证

从后端目录执行 `.venv/bin/python -m unittest discover -s tests`。普通测试无需模型调用；真实 PostgreSQL 测试需要显式配置以下环境变量，未配置时会跳过对应集成测试：

| 环境变量 | 用途 |
| --- | --- |
| `WORKFLOW_TEST_DATABASE_URL` | 真正的 LangGraph/PostgresSaver、并发审核、断连及恢复 |
| `GLOBALPILOT_RAG_TEST_DATABASE_URL` | PGVector 原子写入、取消、MVCC、单连接池 |
| `EVAL_TEST_DATABASE_URL` | JSON 合并、汇总、唯一基线、租约、只读快照 |
| `CONCURRENCY_MIGRATIONS_TEST_DATABASE_URL` | 旧 schema 升级、历史重复基线处理 |
| `AUTH_CONCURRENCY_TEST_DATABASE_URL` | Google 身份竞争绑定、幂等与异常重试 |
| `STARTUP_TEST_ADMIN_URL` | 四进程冷启动；需有创建数据库权限，会创建并清理专用临时库 |

仅指向可丢弃的专用 PostgreSQL 测试库；RAG 测试库名需以 `test_`、`audit_` 或 `refactor_` 开头，并支持 `vector` 扩展。测试使用确定性的本地模型/embedding 替身，SQL、事务、MVCC、锁与 checkpoint 是真实执行；测试成功不代表已完成线上模型或浏览器验收。

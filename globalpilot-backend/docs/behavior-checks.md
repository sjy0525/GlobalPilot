# 后端异步、事务与并发行为检查

检查日期：2026-10-16。检查使用当前项目代码、独立 PostgreSQL/PGVector 与临时 HTTP 服务；没有修改业务库数据或迁移业务 schema。

## 结果

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 上传/PDF 解析期间其他接口响应 | 普通 PDF 可继续响应；实测发现高片段数上传阻塞，修复后通过 | 真实单 worker Uvicorn + TCP；`/` 和 `/api/brands` 并发探测、5ms heartbeat |
| 业务写入中途失败 | 单个数据库事务内的文档、评分、基线等写入正确回滚 | 执行真实 SQL 后注入 `SELECT 1 / 0` 等故障，校验前后数据与完整状态 SHA256 |
| 两个请求同时修改 | 文档/审核旧版本被拒绝；独立评分字段合并；基线保持唯一 | 请求 barrier、真实 PostgreSQL 锁等待、最终数据库状态 |

## 1. 异步阻塞：发现并修复一处实际问题

使用真实应用 lifespan、路由、中间件、multipart 上传、PDF 子进程解析、文本切分与 PostgreSQL 写入。Embedding 使用确定性的 1024 维本地替身，避免真实供应商响应与费用干扰测试；另单独模拟 500ms 网络等待。

问题输入仅 **42,000 字节**：`b'x\n\n' * 14000`。切分后形成 14,000 个片段。旧实现把全部片段拼成一个 INSERT，每行五个参数，共 70,000 个绑定参数，超过 PostgreSQL 的 65,535 上限。构造 SQL 与适配向量参数同时占用事件循环，结果为上传 HTTP 500、其他接口延迟约 1.69 秒。`async def` 或 `await db.execute()` 不会自动把语句构造、参数序列化移出事件循环。

修复在 `app/modules/rag/retrieval/vector_store.py`：同步与异步入口共用 **64 条/批**的构造方式，每批仍使用传入的同一个 Session。每批 awaited I/O 提供调度机会，参数数量和单次编译/适配成本都有界；文档更新、删除旧向量与全部新批次仍由一个外层事务提交，没有分批 commit。

同样输入修复后返回 201，并从数据库直接确认 14,000 个向量全部保存。最终完整矩阵还加入 8MiB TXT（触发磁盘暂存上传和大文本子进程切分），保存 20,263 个向量；真实 PDF 为40页、8,000次文本提取，单次480个片段，还测了两个 PDF 同时上传。

| 本地控制场景 | `/api/brands` P95 | 最大响应时间 | 结果 |
| --- | ---: | ---: | --- |
| Embedding 等待500ms，两轮 | 4.13–4.62ms | 4.70ms | 均201；等待期间其他请求完成 |
| 真实 PDF，两轮 | 10.26–11.19ms | 11.71ms | 均201；解析发生于独立 PID |
| 两个真实 PDF 并发 | 10.56ms | 10.80ms | 两个均201，960个向量 |
| 8MiB TXT | 13.10ms | 22.05ms | 201，20,263个向量 |
| 14,000短段落，修复前 | 1,691.70ms | 1,691.70ms | 500，超出参数上限 |
| 14,000短段落，修复后 | 11.85ms | 79.86ms | 201，14,000个向量 |

最终单次矩阵中，正常场景 heartbeat 最大延迟87.63ms。验证阈值为探测请求 P95<100ms、最大值<150ms、heartbeat 最大值<100ms，均满足；这是本地控制负载的检查标准，不是生产 SLA。

另外故意让测试 Embedding 替身同步 `sleep(0.3)`，实测接口延迟297.51ms、heartbeat302.72ms。这个负对照确认检测能够发现阻塞；它仅存在于验证脚本，不是应用新增的行为。

证据：[修复前](verification/http-responsiveness-before.json)、[最终矩阵](verification/http-responsiveness.json)。复跑入口：`scripts/check_http_responsiveness.py --help`，必须明确提供可丢弃的 test_/audit_/refactor_ 数据库；脚本会启动完整测试应用并在该库执行初始化。

## 2. 事务：在真正写入之后注入失败

RAG HTTP 检查共7个测试、9个场景，其中6个故障场景分别为：POST/PUT 正文已经 flush 后、向量删除与新向量插入后，以及 DELETE 仅删向量后、两个表都实际删除后。注入真实 SQL 错误，HTTP 均为500；随后使用新 Session 读取，确认正文、原始 bytes、版本、时间戳、向量 ID/内容/embedding/metadata 的完整状态 SHA256 与操作前一致。

评测 HTTP 检查覆盖 task/summary、baseline、run/tasks/lease 三种已经执行 SQL 后的失败：请求500，写入与派生状态一起回滚。

分批写入另有真实1024维集成检查：第一批已插入，第二批执行 SQL 失败。同步、异步两条入口都确认旧文档、旧文件、旧版本和旧向量恢复，第一批新向量没有残留。

**工作流需要单独说明边界**：LangGraph checkpoint 与 Brief 投影是分步提交，不能保证整张图的一次调用全回滚。测试确认：投影 UPDATE 后、提交前失败，仅投影事务回滚；已经持久化的审核决定与 checkpoint 保留。GET 返回当前 checkpoint，POST 恢复投影且不会再次调用图；checkpoint 中途失败后，已保存的审批也不能被相反审核覆盖。这里验证的是持久化进度与恢复协议，不是跨两个独立事务的原子性。

## 3. 并发：证明请求确实重叠

- 两个文档 PUT 在 embedding barrier 同时就绪，并从 PostgreSQL 确认事务锁等待。相同 `expected_version` 只有一个200，另一个409；最终文档、版本与所有向量完全属于胜者，没有混合片段。
- PUT/DELETE 两种交错分别得到200/409或200/404；最终没有孤立向量，删除后的文档不会被迟到更新复活。
- 两个评分 PATCH 均200，不同字段同时保留；修改不同任务时，汇总和报告与全部已提交评分一致。
- 两次 baseline 切换均可成功，最终恰好一个基线。
- 同一稿件并发审核得到200/409，成功反馈保留。旧审批已经通过 HTTP preflight 后被并发新稿取代，写入阶段仍返回409，没有批准新稿。

评测/工作流新增10个真实 TCP HTTP 测试，共33次请求；RAG 事务/并发新增7个测试使用 ASGITransport，包含真实路由、multipart、错误处理、Session 与数据库，**没有把 ASGITransport 描述成实际网络测试**。异步响应矩阵则使用独立进程中的实际 TCP 服务。JWT、外部模型与检索内容由受控替身提供，这些结果不证明第三方服务可用性或生产吞吐。

证据：[RAG HTTP故障与并发](verification/rag-http.json)、[评测/工作流TCP检查](verification/evaluation-workflow.json)。对应测试入口为 `tests/test_http_rag_checks_pg.py`、`tests/test_http_evaluation_checks_pg.py`、`tests/test_document_chunk_batches.py`。

## 业务环境边界

只读 schema 预检查仍确认业务库缺少 `briefs.workflow_version`、`briefs.workflow_checkpoint_id`、`knowledge_documents.version` 与 `eval_executor_leases`。隔离库已经初始化新 schema，因此不能把这些测试通过视作业务环境部署通过。升级步骤见 [concurrency.md](concurrency.md)。本次没有迁移、清空或写入业务库。

[业务 schema 只读证据](verification/business-schema-preflight.json)。临时 HTTP 服务和隔离 PostgreSQL 实例在检查完成后关闭清理，业务数据库容器保持原运行状态。

最终完整回归：**283 项通过，0 失败、0 错误、0 跳过**；所有真实 PostgreSQL 测试环境变量均已启用。另有独立实际 TCP 异步响应矩阵全部通过，且确认测试 HTTP 服务、PDF 子进程已经退出。[汇总证据](verification/final-validation.json)。

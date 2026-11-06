# 实际查询、索引与 EXPLAIN 验证

本次同时验证了当前业务库与隔离扩容数据。SQL 来自实际服务查询和安装的 PGVector / PostgresSaver，未通过关闭顺序扫描或添加接口不存在的 LIMIT 改变计划。

## 业务库现状

项目配置的 `127.0.0.1:5434` 最初拒绝连接。确认已有 `globalpilot-postgres` 容器停止后，启动该容器并重连成功。未重建容器、删除卷或启动后端。业务库检查使用只读事务；没有执行 DML、DDL、VACUUM 或 ANALYZE。

当前 PostgreSQL 17.10、pgvector 0.8.5；精确计数为 4 个用户、9 条 Brief、6 份文档、9 个向量片段、2 次评测、60 个评测任务、84 个 checkpoint。`pg_stat_user_tables` 的估算计数均为 0、ANALYZE 时间为空，因此没有把估算计数当成空表证据。小表选择顺序扫描通常合理，不能由此认定缺索引。

业务 schema 尚未应用上轮并发迁移：缺少 `briefs.workflow_version/workflow_checkpoint_id`、`knowledge_documents.version`、`eval_executor_leases`，基线唯一索引也未安装。这是代码与数据库版本不一致，不能通过增加性能索引解决。只读审计明确记录无法在旧 schema 上执行的当前查询，没有偷偷迁移或用改写 SQL 冒充成功。

目录、列和索引证据：[business-catalog.json](index-audit/business-catalog.json)。当前业务 SQL 的计划与缺列说明：[business-plans.json](index-audit/business-plans.json)。

业务库实际执行 12 条 SELECT 计划，每条 3 轮。例如，两种 RAG 过滤写法在仅 9 个片段时均为约 0.070 ms；最新 checkpoint 的完整查询约 0.099 ms。这里只验证查询与既有索引行为，不声称这些小样本证明了扩容收益。当前完整 Brief ORM 查询的缺列另作为阻塞项记录。

## 已修复的工程问题

### 1. 有 GIN 索引，但实际过滤表达式用不上

安装的 PGVector 将普通 metadata 等值生成 `jsonb_path_match(cmetadata, path, vars)`。现有 `jsonb_path_ops` GIN 并不自动支持这个函数谓词。只创建新索引而不改变查询，仍然扫描全部片段。

品牌标识是应用生成的 UUID 字符串，因此改成 singleton `$in`，让原生 PGVector 生成 `cmetadata ->> 'brand_id' IN (...)`；文档读取、删除使用一致的 `->>` 等值表达式。增加：

```sql
CREATE INDEX CONCURRENTLY ix_embedding_collection_brand_document
ON langchain_pg_embedding
  (collection_id, (cmetadata ->> 'brand_id'), (cmetadata ->> 'document_id'));
```

在 40,000 个 1024 维向量、100 个品牌的合成数据上，五轮热缓存中位数：

| 查询 | 原 EXPLAIN 执行时间 | 修改后 | 计划证据 |
| --- | ---: | ---: | --- |
| 品牌内精确 cosine top20 | 4.417 ms | 0.717 ms | 扫描 40,000 / 过滤 39,600 行，变为索引访问 400 行；heap buffer hits 4,032 → 43 |
| 文档向量文本读取 | 4.405 ms | 0.003 ms | buffer hits 4,032 → 4 |
| 文档片段删除 | 2.334 ms | 0.015 ms | 使用完整 collection + brand + document 索引条件 |

删除的 `EXPLAIN ANALYZE` 会真正执行，故仅在隔离库中测量并回滚。业务库没有执行该操作。检索仍为精确距离计算，改写前后 top20 顺序和距离一致；没有引入 ANN。品牌越少、单品牌片段越多，收益可能越小，距离计算仍随该品牌片段数增长。

### 2. 评测列表存在 1 + 2N 查询

原实现读取 N 条运行后，逐条执行两个 COUNT。即使 COUNT 各自走索引，也无法消除数据库往返。现在一次 `GROUP BY run_id` 同时计算总数与完成数，并用 UUID 数组绑定运行集合；非空列表固定为 2 条业务 SELECT，空运行仍返回 `(0, 0)`，读取保持同一 MVCC 快照。

进一步增加覆盖计数所需列的索引：

```sql
CREATE INDEX CONCURRENTLY ix_eval_tasks_run_status
ON eval_tasks (run_id, execution_status);
```

在 10,000 次运行、300,000 个任务、100 用户的合成数据上，热用户有 2,000 次运行，三轮中位数：

| 方案 | 业务 SELECT 数 | 查询、ORM 与 DTO 构建耗时 |
| --- | ---: | ---: |
| 原代码与索引 | 4,001 | 550.406 ms |
| 仅新增任务状态索引 | 4,001 | 527.543 ms |
| 批量统计、原索引 | 2 | 80.042 ms |
| 批量统计、新索引 | 2 | 27.587 ms |

最后两组的 EXPLAIN 数据库执行时间合计为 **51.188 → 4.597 ms**，与上表口径不同。聚合计划转为 `Index Only Scan`，读取 60,000 个任务，Heap Fetches=0，shared buffer accesses 为 50,203 → 5,011。测试数据已 VACUUM、静态且可见性良好；高频更新时可能需要额外回表，不能直接承诺相同生产延迟。

### 3. 列表读取并未返回的大字段

Brief 列表原先选择整条 ORM 对象，包含文案、策略和最终输出，但 `BriefListItem` 不返回这些字段。文档列表同样多取正文和原始文件。现在只投影响应需要的列，并验证序列化不会额外懒加载。接口字段与全量返回行为不变。

普通 `EXPLAIN ANALYZE` 并不完整反映 TOAST 解压、客户端解码和网络传输；本次额外测量实际 fetch，并在 Brief 计划中使用 `SERIALIZE TEXT`。

| 合成场景 | 原实际 fetch | 收窄字段后 | 返回值体积估算 |
| --- | ---: | ---: | ---: |
| Brief 热用户 4,500 行 | 50.438 ms | 6.354 ms | 22.51 MB → 1.61 MB |
| 文档列表 40 行 | 2.961 ms | 0.142 ms | 988,628 → 5,188 bytes |

文档列表的纯 EXPLAIN 执行时间约 0.015 → 0.016 ms，没有将字段裁剪虚报为索引扫描提速。

### 4. 单列用户索引还会扫描大量未进入工作流的 Brief

真实过滤条件是 `user_id = :id AND workflow_status IS NOT NULL`，排序为 `updated_at DESC`。增加部分索引：

```sql
CREATE INDEX CONCURRENTLY ix_briefs_user_updated_active
ON briefs (user_id, updated_at DESC)
WHERE workflow_status IS NOT NULL;
```

在 40,000 Brief 的合成分布中，一个用户有 6,000 行但只有 60 行进入工作流。已收窄字段的查询从 **0.401 → 0.058 ms**，避免再过滤 5,940 条未进入工作流的记录。热用户的改进主要来自字段裁剪：实际 fetch 在裁剪后从 6.354 进一步降至 6.238 ms。真实计划仍选择 `Bitmap + Sort`，不能声称复合索引必然消除了排序。

## 没有采用的索引与边界

- `eval_runs(user_id, created_at DESC, id DESC)` 在本次实际全量查询计划中均未被采用，没有把暖机或计时波动当成索引收益，因此未安装。
- User 的主键、email/google_sub 唯一索引已覆盖点查；Brief 通过唯一 id 读取或版本校验，不需要重复增加 `(id, user_id)`、`(id, workflow_version)`。
- PostgresSaver 的最新状态查询使用完整原生 SELECT，包括 blobs 与 pending writes。现有 checkpoints/blobs/writes 复合主键已覆盖其条件；没有重复添加反向排序索引，也没有擅自删除第三方库建立的 thread_id 索引。
- 没有按字段数量机械增加索引，也没有删除现有 GIN、外键单列索引；新索引有磁盘、写入和维护成本。
- 列表依然没有分页，评测列表仍返回历史报告，运行中的前端仍每 2 秒轮询。索引不能消除无限增长的返回量；后续需要分页、轻量列表 DTO 和前端统计一起调整，不能只在后端偷偷 LIMIT 导致数据丢失。
- 未把本次合成热缓存结果当成业务库延迟或线上容量结论。实际用户分布、表大小、并发、缓存命中率、可见性映射都会影响计划。

## 安装与复跑

索引定义集中在 `app/db/query_indexes.py`。新版本应用启动时，在现有跨进程 schema 锁内、业务 schema 事务提交后，以独立 autocommit 连接执行 `CREATE INDEX CONCURRENTLY`。这能为既有表补索引，避免仅修改模型而 `create_all()` 不升级旧表的问题。

重复执行会核对索引的表、键、排序、谓词、collation、operator class 和有效性。同名错误定义或失效索引会明确失败，不会因 `IF NOT EXISTS` 静默误判已安装；也不会自动删除现有索引。并发建索引可能等待旧事务，运维应检查长事务及 invalid index 后再重试。

业务库本次只做审计，**新索引和此前并发迁移均未应用**。先完成并发升级步骤，再可显式安装这些索引：

```sh
QUERY_INDEX_DATABASE_URL='显式目标数据库 URL' \
  .venv/bin/python scripts/apply_query_indexes.py
```

可复跑脚本与原始证据：

- `scripts/audit_business_query_plans.py` → [business-plans.json](index-audit/business-plans.json)：只读、业务数据、参数脱敏。
- `scripts/audit_rag_indexes.py` → [rag.json](index-audit/rag.json)：原生 async PGVector SQL，五轮计划、精确结果一致性、prepared plan 检查。
- `scripts/audit_eval_indexes.py` → [eval.json](index-audit/eval.json)：实际 ORM SQL、查询次数、冷热用户与索引对照。
- `scripts/audit_workflow_indexes.py` → [workflow.json](index-audit/workflow.json)：真实无 LIMIT 列表、投影、Auth、完整 checkpoint 查询。

RAG、评测脚本只接受显式测试数据库，创建并清理自己的随机 schema。Workflow 脚本从显式管理连接创建独立的 `refactor_indexes_workflow` 数据库，默认拒绝覆盖已存在的同名库；`--reset` 仅用于明确重建这个测试库。请先看各脚本 `--help`，不要指向业务集群。不调用模型或 embedding 服务。集成测试新增 `QUERY_INDEX_TEST_DATABASE_URL`，验证索引升级、重复运行、并发安装及错误定义拒绝。

本轮完整后端回归 **263 项通过、无跳过**，真实 PostgreSQL 集成环境变量均已启用。测试数据库使用独立临时实例，默认 `DATABASE_URL` 也覆写为该实例，避免测试误连已恢复的业务库。包含四进程首次启动、单连接池、旧 schema 升级、同名错误 collation/operator class 拒绝，以及检索结果和列表响应兼容验证。

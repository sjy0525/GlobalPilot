# 查询索引与 EXPLAIN 审计历史记录

这份记录来自一次本地测试，包含开发库的只读检查和隔离数据库中的合成数据基准。下文的库状态、执行计划和耗时均对应当时的快照。审计使用当时的服务查询、PGVector 和 PostgresSaver 生成的 SQL，保留顺序扫描选项及接口原有的返回范围。

## 本地开发库的只读检查

审计在只读事务中记录当时的表结构、索引和可执行查询的计划。开发库样本较小，统计信息尚未刷新，判断数据量时采用精确计数。小表选择顺序扫描通常合理，是否需要索引还要看扩大数据量后的计划。

当时的开发库 schema 落后于应用代码。计划报告列出了因缺少列或表而无法执行的查询，读库检查期间没有应用迁移。性能对照以隔离库的合成数据为准。

历史表结构和索引记录在当时本地生成的 `index-audit/business-catalog.json`，查询计划及执行限制记录在 `index-audit/business-plans.json`。

## 当时的优化项与合成数据结果

### 1. 有 GIN 索引，但实际过滤表达式用不上

当时使用的 PGVector 将普通 metadata 等值生成 `jsonb_path_match(cmetadata, path, vars)`。原有的 `jsonb_path_ops` GIN 并不自动支持这个函数谓词。保留原查询表达式时，查询仍扫描全部片段。

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

删除的 `EXPLAIN ANALYZE` 会实际执行删除，因此仅在隔离库中测量并回滚。对照中的向量检索使用精确距离计算，改写前后 top20 的顺序和距离一致。距离计算量随单品牌片段数增长，数据向少数品牌集中时收益可能较小。

### 2. 评测列表存在 1 + 2N 查询

原实现读取 N 条运行后，逐条执行两个 COUNT。即使 COUNT 各自走索引，也无法消除数据库往返。改动用一次 `GROUP BY run_id` 同时计算总数与完成数，并用 UUID 数组绑定运行集合。非空列表固定为 2 条业务 SELECT，无任务的运行仍返回 `(0, 0)`，读取保持同一 MVCC 快照。

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

Brief 列表原先选择整条 ORM 对象，包含 `BriefListItem` 响应之外的文案、策略和最终输出。文档列表同样多取正文和原始文件。改动将投影收窄到响应需要的列，并验证序列化没有额外懒加载，响应仍包含原有字段和完整列表。

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
- 当时测量的是无分页列表，评测列表包含历史报告。索引只能优化查询过程，响应体积仍随记录数增长。分页需同步调整列表 DTO 和前端统计，才能维持完整的数据访问。
- 合成数据的热缓存耗时仅用于对照方案。实际用户分布、表大小、并发、缓存命中率和可见性映射都会影响计划。

## 安装与复跑

索引定义集中在 `app/db/query_indexes.py`。安装流程在跨进程 schema 锁内、业务 schema 事务提交后，以独立 autocommit 连接执行 `CREATE INDEX CONCURRENTLY`，为既有表补建索引。

重复执行会核对索引的表、键、排序、谓词、collation、operator class 和有效性。同名错误定义或失效索引会明确失败，不会因 `IF NOT EXISTS` 静默误判已安装；也不会自动删除现有索引。并发建索引可能等待旧事务，运维应检查长事务及 invalid index 后再重试。

当时的开发库只用于只读检查。需要手动安装时，先确认目标数据库已应用对应的 schema 迁移，再用以下命令指定连接并安装索引。

```sh
QUERY_INDEX_DATABASE_URL='显式目标数据库 URL' \
  .venv/bin/python scripts/apply_query_indexes.py
```

### 复跑脚本与历史报告

以下脚本在本地生成 JSON 报告。报告路径相对于本文档所在目录。

- [业务查询审计脚本](../scripts/audit_business_query_plans.py) 生成 `index-audit/business-plans.json`，记录只读查询计划并脱敏参数。
- [RAG 索引审计脚本](../scripts/audit_rag_indexes.py) 生成 `index-audit/rag.json`，记录原生异步 PGVector SQL 的五轮计划、精确结果一致性和 prepared plan 检查。
- [评测索引审计脚本](../scripts/audit_eval_indexes.py) 生成 `index-audit/eval.json`，记录 ORM SQL、查询次数、冷热用户和索引对照。
- [工作流索引审计脚本](../scripts/audit_workflow_indexes.py) 生成 `index-audit/workflow.json`，记录无 `LIMIT` 的真实列表查询、字段投影、鉴权和完整 checkpoint 查询。

RAG 和评测脚本要求显式指定测试数据库，各自在随机 schema 中生成并清理数据。Workflow 脚本创建独立的测试数据库，`--reset` 用于明确重建同名测试库。复跑前可用 `--help` 确认各脚本要求。合成基准无需模型或 embedding 服务。

历史后端回归在独立 PostgreSQL 实例中运行，覆盖并发启动、单连接池、旧 schema 升级、错误索引定义拒绝，以及检索结果和列表响应兼容。索引集成测试通过 `QUERY_INDEX_TEST_DATABASE_URL` 指定测试库。

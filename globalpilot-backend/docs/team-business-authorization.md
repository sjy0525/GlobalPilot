# 团队业务接口鉴权

本次覆盖资料与知识库 7 个接口、Brief 2 个接口、营销工作流 5 个接口、记忆查询 1 个接口、评测中心 10 个接口。原接口地址保留，统一要求显式的 `team_id` 查询参数。

```http
GET /api/brands?team_id=<team_uuid>
GET /api/documents?team_id=<team_uuid>&brand_id=<brand_uuid>
POST /api/briefs?team_id=<team_uuid>
GET /api/briefs/workflow?team_id=<team_uuid>&brief_id=<brief_uuid>
GET /api/briefs/<brief_uuid>/memories?team_id=<team_uuid>
GET /api/evals/runs?team_id=<team_uuid>
Authorization: Bearer <access_token>
```

更新、删除资料原有的 `expected_version` 仍然必填；其他原有业务参数保持不变。上传和评测创建返回的 `Location` 包含后续请求必需的团队参数。

## 权限规则

- 复用现有 JWT 和 `get_current_user`。`get_current_member_from_query` 使用数据库中的账号状态、成员状态及 `admin/employee` 角色，不读取 JWT 中的角色。未登录 401，非有效成员或停用账号 403；已登录但缺少/传错团队 UUID 为 422。
- 资料和知识库在有效团队成员间共享，两个角色都可进行当前团队的原有业务操作。品牌、文档行、向量块均限制团队，文档还需属于所选品牌。无关团队、历史未归属及不存在的资源统一按 404 处理。
- Brief、工作流、Brief 记忆和评测运行继续保留创建人规则：必须同时满足团队归属及个人归属。管理员对全团队工单的权限不扩展到其他人的营销需求和评测。
- 评测用例定义可以共用，但读取也要求当前团队的有效成员身份。知识库指纹、评测运行、任务、对比、报告及基线按团队隔离；每个团队可有各自基线。
- 创建方、账号、成员状态、团队及记忆范围均由后端确定。创建请求不能通过业务 JSON 覆盖身份和角色。
- 写入采用短事务，按团队再业务对象顺序加锁，获得锁后重新读取成员状态；外部模型、Embedding 和重排序等待期间不持有团队锁。长操作完成及工作流流式输出时再次检查权限，撤权后不返回新结果或发布业务投影。
- 记忆与历史快照使用 `v2 + team_id + user_id + brand_id` 范围。后台队列从持久化的 Brief 得到团队，校验账号、成员、品牌及创建人，并在工具执行前复查。

## 数据库升级和历史数据

正常启动在 `_prepare_schema` 中先建立团队父表并升级已有表的团队字段，再创建缺失业务表，最后执行已有 `apply_concurrency_migrations` 补齐并验证整个模式。这也支持旧库已有品牌表但缺少部分业务表的状态。升级在现有启动事务和数据库互斥锁内执行，无需额外迁移框架。

升级为 brands、knowledge_documents、briefs、eval_runs 增加可空的 `team_id` 外键。可空仅用于保留历史数据；新的业务创建必须指定经校验的团队。品牌名称改为团队内唯一，文档和 Brief 的组合外键确保其品牌属于同一团队。评测新旧基线分别使用 partial unique index；遇到同名但定义错误的索引或团队外键会拒绝升级，保留原数据供核对。

启动为已有团队创建独立的 Wearables、Audio、Smart home 分类；受控首个管理员初始化也会创建该团队分类。同名分类在不同团队具有不同 ID。

不会根据用户当前加入的团队自动推断历史归属，也不会把全局资料或记忆复制到所有团队：

1. 先确认旧品牌、文档、Brief 和评测各自的实际归属。一个旧品牌被多团队共用时，需要受控拆分品牌和相关资源，而不是直接指向新建的默认分类。
2. 在受控数据库事务中按确认的映射回填，先处理品牌，再处理引用它的文档及 Brief；组合外键会拒绝不一致的映射。
3. 历史向量块还需要经确认写入相同 `team_id`，并与实际文档和品牌一致；只有修改文档行不会让无团队的旧向量自动进入检索。
4. 旧 v1 个人品牌记忆及历史快照保留在原 namespace。其归属需要根据来源任务确认，不能自动合并或复制到新团队范围。
5. 非空工作流 checkpoint 必须包含匹配的 `team_id`、`actor_user_id`。旧无范围 checkpoint 返回 409，不能在回填 Brief 后直接恢复并输出旧的全局检索上下文。应核对来源并受控重新生成流程。

尚未确认归属的历史行继续保留，但不通过团队业务接口开放。本次没有连接或改写日常业务数据库，升级执行与实际历史归属回填是两个独立步骤。

## 前端调用约定

前端需要在这 25 个接口的请求和文件下载地址中附上当前团队 ID，切换团队后重新读取对应范围的数据；不能只把团队 ID 放在 JSON 中。本次仅修改后端，未修改前端。

## 验证

使用随机端口的独立 PostgreSQL/pgvector 容器，数据库名称为 `test_team_business`；外部 Embedding、LLM、重排序及搜索调用使用本地替身。完整应用 HTTP 验证使用正常数据库依赖和真实密码登录签发的 JWT，仅关闭后台/外部资源 lifespan，以便独立验证 HTTP 业务。

自动回归报告见 `test-results/team-business-authorization/README.md`。没有使用浏览器，也不包含线上部署或真实外部模型质量验收。

# 定向改稿 Agent 后端

改稿任务面向一份营销任务中一个平台的 `body`，使用后端固定的段落 ID 和原稿版本。Hook、CTA、标签、其他平台、只读段落和段落间分隔符不属于修改范围。

## 执行流程

读取授权原稿与品牌证据 → 首次改稿 → 程序检查 → 语义检查 → 最多三次自动修正 → 人工审核 → 版本校验并采纳 → 保存业务样本。

缺资料时暂停等待补充；审核执行失败、修正失败或预算耗尽时保留当前候选和问题。人工拒绝结束本任务并记录反馈，可以用新要求创建下一项任务。审核和补充信息通过 PostgreSQL 检查点恢复，不能把重新运行当作重置修订次数。

## HTTP 接口

以下路径统一带 `/api` 前缀，并使用现有登录凭证及 `team_id` 查询参数。当前权限沿用营销任务规则：有效团队成员且是营销任务创建者。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/briefs/{brief_id}/revisions/document?platform=...` | 读取正文快照、稳定段落 ID 和版本 |
| POST | `/briefs/{brief_id}/revisions` | 创建改稿任务 |
| GET | `/briefs/{brief_id}/revisions` | 查询此营销任务的改稿记录 |
| GET | `/revision-tasks/{task_id}` | 查询任务、候选、检查报告及等待原因 |
| POST | `/revision-tasks/{task_id}/run` | 首次执行或恢复中断执行 |
| POST | `/revision-tasks/{task_id}/clarification` | 提交补充信息并恢复 |
| POST | `/revision-tasks/{task_id}/review` | 审核、拒绝或采纳人工最终文本 |
| GET | `/revision-tasks/{task_id}/sample` | 导出本任务最终采纳的业务样本 |

创建任务先读取 document 接口，再使用返回的 ID 和版本：

```json
{
  "platform": "instagram",
  "expected_draft_id": "从 document 接口取得的 draft_id",
  "expected_draft_version": 1,
  "requirements": {
    "goal": "只精简指定段落，保留参数与售后信息",
    "allowed_block_ids": ["从 document 接口取得的 block_id"],
    "must_keep": ["65W"],
    "length_limits": []
  },
  "brand_rules": ["表达自然，避免夸张承诺"],
  "evidence_document_ids": [],
  "max_revisions": 3,
  "idempotency_key": "create-my-revision-001"
}
```

`brand_rules` 是本任务的表达约束，不是产品事实。事实证据只能由后端从当前团队、当前品牌资料读取。指定 `evidence_document_ids` 时，后端仍检查权限、版本和引用原文，不接受客户端伪造证据内容。

执行与后续命令必须携带刚读取的版本和检查点：

```json
{
  "expected_task_version": 1,
  "expected_checkpoint_id": null,
  "idempotency_key": "run-my-revision-001"
}
```

首次运行前检查点为 null。之后使用任务响应中的 `version` 与 `checkpoint_id`。相同幂等键和相同内容重放同一操作；相同键配不同内容返回冲突。不同命令也必须使用不同幂等键。

补充信息在命令字段之外提供 `feedback`，并可提供 `goal`、`must_keep`、`length_limits`、`evidence_document_ids`。原稿及允许修改范围不能由补充信息替换；原始要求保留，当前要求和补充历史单独记录。用户补充文字不能自动升级为品牌事实。

审核在命令字段之外提供 `action: "approved" | "rejected"`。拒绝必须提供 `feedback`。采纳人工编辑时可提供 `blocks`：整份候选的段落列表，包含原有段落 ID。后端重新检查范围、保留项、长度和语义，不允许借人工编辑修改只读内容。

## 版本和营销父流程

段落快照版本独立于营销流程的检查点版本。采纳时同时校验原稿、当前营销检查点、完整原文及证据版本。旧任务不会自动覆盖新营销稿件。

通过的局部修改会合并回完整营销文案，完整文案再检查一次；这个检查失败时不会触发全文重写。成功后同步父流程检查点和 Brief 投影，营销父流程回到整份文案的人工审核节点。改稿任务的“已采纳”表示指定段落已采纳进当前营销草稿，并不代替整份营销稿件的最终发布批准。

## 数据与恢复

业务表为 `marketing_revision_drafts`、`marketing_revision_tasks`、`marketing_revision_operations` 和 `marketing_revision_samples`，通过现有模型注册及启动建表机制创建。图检查点使用现有 PostgreSQL checkpoint 表，改稿任务 UUID 是独立线程 ID；写入仍受对应营销任务的物理数据库会话锁保护。

样本只在真实人工采纳后产生，关联原稿、原始及当前修改要求、品牌规则、冻结证据、执行检查历史、模型候选和人工最终采纳文本。被拒绝、预算耗尽和执行失败的候选不会作为已采纳样本导出。样本仍需业务评测与筛选，保存记录不等于已经可以直接用于微调。

## 测试

```bash
./.venv/bin/python -B -m unittest discover -s tests -p 'test_marketing_revision*.py'
```

PostgreSQL/HTTP 验证需显式提供隔离测试数据库；相关测试拒绝非测试数据库，并使用独立 schema。单元测试通过不代表真实模型的业务判断已经通过验收。

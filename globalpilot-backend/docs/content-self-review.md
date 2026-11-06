# 文案模型自审

流程：`retrieve_context → generate_strategy → generate_content → self_review`；自审通过后才进入 `human_review`。未通过时带着上一份报告自动回到 `generate_content` 改稿，最多自动重试 3 次；仍未通过则进入 `self_review_exhausted`，不会把不合格稿交给人工批准。

人工退回沿用原有改稿循环和三次修改上限；每一版新文案重新自审，人工批准后才能进入最终输出。

- `app/modules/marketing/workflow/copy.py`：生成节点输出分平台 JSON（hook / body / CTA / tags，不含配图或镜头）。代码校验平台覆盖、字段非空，以及 X 发布文本的 280 Unicode 字符项目预算。该预算以 Hook、正文、CTA、标签换行连接计算，并非 X 官方加权字符算法。
- 分平台结构化文案、审核报告及审核 token 以当前 LangGraph checkpoint 为准；`briefs.content` 是以 JSONB 保存的投影，写入时校验 checkpoint 与版本，不能用过期投影覆盖当前稿件。`final_output` 只是展示文本。并发与恢复协议见 [concurrency.md](concurrency.md)。
- `app/modules/marketing/workflow/self_review.py`：复用现有 DeepSeek 配置，但发起独立模型调用，只核品牌事实；不把生成策略或人工意见作为品牌事实依据。平台格式不再让模型从散文里摘录。
- 品牌事实：逐条抽取文案主张（包括 CTA 和标签中的事实），返回 `supported / unsupported / contradicted`、资料原文及修改建议。代码核对主张确实在结构化文案中、支持依据确实在检索资料中；语义支持和事实覆盖由模型判断，仍可能漏审。
- 平台格式：`PLATFORM_RULES` 覆盖 TikTok、Instagram、YouTube、LinkedIn、Facebook、X。生成和格式校验读取同一份版本化规则。
- 这些是项目的交付模板，未接入平台实时官方限制或发布 API 校验。更新规则时更新 `COPY_VERSION`，旧报告不能批准新规则下的草稿。
- `app/modules/marketing/schemas/content_review.py` 定义事实审查的 JSON 数据结构；最终状态由代码计算，不采信模型自行给出的“通过”。格式错误、模型调用失败或未完成均不能批准，并计入自动重试。
- 未通过时报告带 `blockers` / `summary` / `stuck_at`，写入 checkpoint，并打 `WARNING` 日志。耗尽后 `final_output` 含同一份卡点说明；前端在 `self_review_failed` 展示失败报告。人工审核态仍只暴露已通过的报告。
- 普通/流式审核接口和图内人工批准都仍检查报告（防御性）。前端在人工审核只展示已通过的自审摘要，并禁用无通过报告的批准按钮。
- 自动/人工退回后，上一份报告作为独立改稿提示传给生成模型；生成后清掉旧报告，再审查新稿。系统不会自动替用户批准，也不静默修改原稿。

## 验证

在具备 `pydantic`、`langchain-core`、`langgraph`、`fastapi`、`sqlalchemy` 的 Python 环境中，从后端目录执行：

```sh
python -m unittest discover -s tests -v
```

测试使用真实 LangGraph 图、内存 checkpoint、模拟模型与检索结果，不读取品牌数据库。
覆盖结构化 JSON 解析、平台覆盖、字段缺项、字符预算、事实无依据/冲突、伪造引用、模型错误、旧报告、自动改稿重审、自审耗尽、人工批准拦截、修改上限和无资料退出。

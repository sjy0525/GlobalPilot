"""评测报告。数字来自当次快照，不回写历史运行的版本。"""


def _pct(rate: float | None) -> str:
    if rate is None:
        return "—"
    return f"{rate * 100:.1f}%"


def _usd(value: float | None) -> str:
    if value is None:
        return "—"
    return f"${value:.6f}"


def _seconds(ms: float | None) -> str:
    if ms is None:
        return "—"
    return f"{ms / 1000:.1f} 秒"


def render_report(run: dict, tasks: list[dict]) -> str:
    provenance = run.get("provenance") or {}
    summary = run.get("summary") or {}
    models = provenance.get("model_settings") or {}
    generator = models.get("generator") or {}
    lines = [
        f"# 评测报告：{run.get('label') or '未命名运行'}",
        "",
        "## 这次评测记住的版本",
        f"- 测试集版本：{provenance.get('test_set_version') or '—'}",
        f"- 工作流版本：{provenance.get('workflow_version') or '—'}",
        f"- Prompt 版本：{provenance.get('prompt_version') or '—'}（{provenance.get('prompt_fingerprint') or '—'}）",
        f"- 模型配置：{generator.get('model') or '—'}，温度 {generator.get('temperature', '—')}",
        f"- 知识库版本：{provenance.get('knowledge_version') or '—'}",
        f"- 评分规则：{provenance.get('scoring_rules_version') or '—'}（{provenance.get('scoring_fingerprint') or '—'}）",
        f"- 成本口径：{(models.get('cost_scope') or '—')}，价卡 {models.get('price_card') or '—'}",
        "",
        "## 合格标准",
        "事实有资料支持、要求的内容没有遗漏、品牌语气符合规范、平台格式满足要求。四项都通过才算合格。",
        "初稿合格率 = 未经返工就合格的任务数 ÷ 全部测试任务数。超时、空输出和报错留在分母里。",
        "",
        "## 结果",
        f"- 任务数：{summary.get('total', len(tasks))}",
        f"- 未经返工就合格：{summary.get('first_draft_passed', 0)}",
        f"- 初稿合格率：{_pct(summary.get('first_draft_pass_rate'))}",
        f"- 事实错误：{summary.get('fact_error_count', 0)} 条（另有待确认 {summary.get('fact_pending_count', 0)} 条）",
        f"- 空输出：{summary.get('empty_count', 0)}，超时：{summary.get('timeout_count', 0)}，报错：{summary.get('error_count', 0)}",
        f"- 耗时合计：{_seconds(summary.get('duration_ms_total'))}，平均：{_seconds(summary.get('duration_ms_avg'))}",
        f"- 生成成本合计：{_usd(summary.get('api_cost_usd_total'))}",
        f"- 单份合格内容成本：{_usd(summary.get('cost_per_qualified_usd'))}",
        "",
        "单份合格内容成本 = 全部任务的生成 API 成本 ÷ 未经返工就合格的任务数。评测模型的打分费用不计入。没有合格任务时不计算。",
        "",
        "## 未合格任务",
    ]
    failed = [task for task in tasks if not task.get("qualified")]
    if not failed:
        lines.append("没有未合格任务。")
    for task in failed:
        lines.append(f"### {task.get('code')} {task.get('title')}")
        flags = []
        if task.get("timed_out"):
            flags.append("超时")
        if task.get("empty_output"):
            flags.append("空输出")
        if task.get("execution_status") == "error":
            flags.append("报错")
        if flags:
            lines.append(f"- 执行：{'、'.join(flags)}")
        if task.get("error"):
            lines.append(f"- 错误：{task['error']}")
        for reason in task.get("failure_reasons") or []:
            lines.append(f"- {reason}")
        if task.get("human_note"):
            lines.append(f"- 人工备注：{task['human_note']}")
    lines.append("")
    lines.append("简历可写的句子需要两次可比运行。单次报告只记录上面的实测数字。")
    return "\n".join(lines)


def render_comparison(left: dict, right: dict, rows: list[dict]) -> str:
    left_summary = left.get("summary") or {}
    right_summary = right.get("summary") or {}
    left_rate = left_summary.get("first_draft_pass_rate")
    right_rate = right_summary.get("first_draft_pass_rate")
    lines = [
        "# 版本对比",
        "",
        f"基线侧：{left.get('label')}（{left.get('provenance', {}).get('workflow_version')} / {left.get('provenance', {}).get('prompt_version')}）",
        f"对照侧：{right.get('label')}（{right.get('provenance', {}).get('workflow_version')} / {right.get('provenance', {}).get('prompt_version')}）",
        "",
        "| 指标 | 左侧 | 右侧 |",
        "| --- | --- | --- |",
        f"| 初稿合格率 | {_pct(left_rate)} | {_pct(right_rate)} |",
        f"| 事实错误 | {left_summary.get('fact_error_count', 0)} | {right_summary.get('fact_error_count', 0)} |",
        f"| 平均耗时 | {_seconds(left_summary.get('duration_ms_avg'))} | {_seconds(right_summary.get('duration_ms_avg'))} |",
        f"| 单份合格内容成本 | {_usd(left_summary.get('cost_per_qualified_usd'))} | {_usd(right_summary.get('cost_per_qualified_usd'))} |",
        "",
    ]
    if (
        isinstance(left_rate, (int, float))
        and isinstance(right_rate, (int, float))
        and left.get("provenance", {}).get("test_set_version")
        == right.get("provenance", {}).get("test_set_version")
    ):
        total = left_summary.get("total") or right_summary.get("total") or 0
        lines.append(
            f"简历句子：建立覆盖 {total} 条营销场景的评测集，通过版本回归评测，将初稿合格率从 {_pct(left_rate)} 提升至 {_pct(right_rate)}。"
        )
        if right_rate < left_rate:
            lines.append("右侧合格率低于左侧，上面这句话先不要写进简历。")
        lines.append("")
    lines.append("## 失败原因")
    failed = [
        row for row in rows
        if not row.get("left_qualified") or not row.get("right_qualified")
    ]
    if not failed:
        lines.append("两侧都没有未合格任务。")
    for row in failed:
        lines.append(f"### {row.get('code')} {row.get('title')}")
        lines.append("左侧：")
        for reason in row.get("left_reasons") or ["合格"]:
            lines.append(f"- {reason}")
        lines.append("右侧：")
        for reason in row.get("right_reasons") or ["合格"]:
            lines.append(f"- {reason}")
    return "\n".join(lines)

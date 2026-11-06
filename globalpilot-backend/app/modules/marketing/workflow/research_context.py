"""Shared input gate for externally researched facts; no model or database IO."""


def strategy_research_inputs(state: dict) -> tuple[dict, dict]:
    """Keep rejected external claims out of the model, retaining originals in State."""
    report = state.get("research")
    validation = state.get("research_validation")
    report = report if isinstance(report, dict) else {}
    validation = validation if isinstance(validation, dict) else {}
    urls = report.get("source_urls")
    verified = (
        report.get("status") == "completed"
        and validation.get("passed") is True
        and not validation.get("issues")
        and isinstance(urls, list)
        and bool(urls)
        and all(isinstance(url, str) and url.startswith(("http://", "https://")) for url in urls)
    )
    if verified:
        return {
            "status": "completed",
            "summary": report.get("summary", ""),
            "source_urls": list(urls),
            "uncertainties": list(report.get("uncertainties") or []),
        }, dict(validation)
    if report.get("status") == "not_needed" and validation.get("passed") is True and not validation.get("issues"):
        return {
            "status": "not_needed",
            "summary": "本任务无需外部调研，请只依据品牌资料和营销任务制定策略。",
            "source_urls": [],
            "uncertainties": [],
        }, dict(validation)
    # Do not copy raw issues or uncertainties either: they can quote the same
    # rejected claims and URLs. The original report remains available for audit.
    return {
        "status": "insufficient",
        "summary": "外部调研不足、缺失或未通过核对，本次策略不能引用外部事实。",
        "source_urls": [],
        "uncertainties": ["市场、人群和场景的外部依据仍需补充验证，相关建议必须标注推断或待验证。"],
    }, {
        "passed": False,
        "issues": ["不满足调研完成且核对通过的条件，外部结论已在进入策略模型前移除。"],
    }


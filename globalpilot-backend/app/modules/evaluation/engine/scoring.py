"""合格标准：四项全部为通过才算合格。待确认不算通过。"""
from app.modules.marketing.workflow.copy import flatten_copy, validate_copy

SCORING_RULES_VERSION = "eval-score-v1"

CRITERIA = (
    "事实有资料支持",
    "要求的内容没有遗漏",
    "品牌语气符合规范",
    "平台格式满足要求",
)

# 字数按 Unicode 字符计。下限避免空壳稿，上限是项目交付预算。
LENGTH_LIMITS: dict[str, dict[str, int | tuple[int, int]]] = {
    "tiktok": {"hook": (4, 80), "body": (20, 800), "cta": (2, 80), "tags": (2, 120)},
    "instagram": {"hook": (4, 150), "body": (20, 2200), "cta": (2, 120), "tags": (2, 200)},
    "youtube": {"hook": (4, 100), "body": (40, 2500), "cta": (2, 120), "tags": (2, 200)},
    "linkedin": {"hook": (4, 220), "body": (40, 3000), "cta": (2, 160), "tags": (2, 200)},
    "facebook": {"hook": (4, 150), "body": (20, 2000), "cta": (2, 120), "tags": (2, 200)},
    "x": {"combined_max": 280, "combined_min": 8},
}

DIMENSIONS = ("facts", "completeness", "tone", "format")
DIMENSION_LABELS = {
    "facts": "事实",
    "completeness": "需求完成度",
    "tone": "品牌语气",
    "format": "平台格式",
}
VERDICT_LABELS = {"pass": "通过", "fail": "不通过", "pending": "待确认"}


def is_empty_content(content: dict | None) -> bool:
    if not content:
        return True
    return not flatten_copy(content).strip()


def check_format(content: dict | None, platforms: list[str]) -> tuple[str, str]:
    errors = list(validate_copy(content, platforms))
    platforms_map = (content or {}).get("platforms") or {}
    if not isinstance(platforms_map, dict):
        platforms_map = {}
    for platform in platforms:
        limits = LENGTH_LIMITS.get(platform)
        item = platforms_map.get(platform) or {}
        if not isinstance(item, dict):
            item = {}
        if limits is None:
            errors.append(f"{platform}：未配置字数规则")
            continue
        if platform == "x":
            combined = "\n".join(
                str(item.get(key) or "") for key in ("hook", "body", "cta", "tags")
            )
            if len(combined) < int(limits["combined_min"]):
                errors.append("x：发布文本短于 8 个字符")
            if len(combined) > int(limits["combined_max"]):
                errors.append("x：发布文本超过项目的 280 字符预算")
            continue
        for field, bound in limits.items():
            if not isinstance(bound, tuple):
                continue
            low, high = bound
            length = len(str(item.get(field) or "").strip())
            if length < low or length > high:
                errors.append(f"{platform}：{field} 字数为 {length}，要求 {low}–{high}")
    # validate_copy already reports the x 280 cap; drop the duplicate.
    deduped: list[str] = []
    seen: set[str] = set()
    for error in errors:
        if error not in seen:
            seen.add(error)
            deduped.append(error)
    if deduped:
        return "fail", "；".join(deduped)
    return "pass", "格式和字数符合平台要求"


def _cell(verdict: str, reason: str, source: str) -> dict:
    return {"verdict": verdict, "reason": reason, "source": source}


def blocked_scores(reason: str) -> dict:
    return {key: _cell("fail", reason, "code") for key in DIMENSIONS}


def score_draft(
    content: dict | None,
    platforms: list[str],
    judge: dict | None,
    blocked_reason: str | None = None,
) -> dict:
    if blocked_reason:
        return blocked_scores(blocked_reason)
    if is_empty_content(content):
        return blocked_scores("空输出")
    verdict, reason = check_format(content, platforms)
    scores = {"format": _cell(verdict, reason, "code")}
    for key in ("facts", "completeness", "tone"):
        item = (judge or {}).get(key) or {}
        raw = item.get("verdict")
        if raw not in ("pass", "fail", "pending"):
            scores[key] = _cell("pending", "评测模型没有返回这一项", "model")
        else:
            text = str(item.get("reason") or "").strip() or "评测模型未说明原因"
            scores[key] = _cell(raw, text, "model")
    return scores


def is_qualified(scores: dict) -> bool:
    return all(
        isinstance(scores.get(key), dict) and scores[key].get("verdict") == "pass"
        for key in DIMENSIONS
    )


def failure_reasons(scores: dict) -> list[str]:
    reasons = []
    for key in DIMENSIONS:
        item = scores.get(key) or {}
        verdict = item.get("verdict")
        if verdict == "pass":
            continue
        label = VERDICT_LABELS.get(verdict, "未评分")
        reasons.append(f"{DIMENSION_LABELS[key]}{label}：{item.get('reason') or '无原因'}")
    return reasons

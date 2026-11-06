"""Structured per-platform copy: parse, hash, and project format checks."""
import hashlib
import json

from pydantic import ValidationError

from app.modules.marketing.schemas.copy import COPY_VERSION, GeneratedCopy, MarketingCopy

# Project delivery formats, not assertions about current platform API limits.
PLATFORM_RULES = {
    "tiktok": "Hook 是开场口播，4–80 字；正文为短视频口播脚本，20–800 字；CTA 2–80 字；标签 2–120 字。",
    "instagram": "标题/Hook 为首句，4–150 字；正文为图片/轮播配文，20–2200 字；CTA 2–120 字；标签 2–200 字。",
    "youtube": "标题/Hook 为视频标题，4–100 字；正文包含视频简介及脚本要点，40–2500 字；CTA 2–120 字；标签 2–200 字。",
    "linkedin": "标题/Hook 为开场句，4–220 字；正文为专业场景的独立帖子，40–3000 字；CTA 2–160 字；标签 2–200 字。",
    "facebook": "标题/Hook 为开场句，4–150 字；正文为可独立发布的动态，20–2000 字；CTA 2–120 字；标签 2–200 字。",
    "x": "标题/Hook、正文、CTA、标签用换行合并为一条短帖，合计不超过 280 个 Unicode 字符（含换行）。建议 hook≤24、正文≤160、CTA≤36、标签≤40。",
}


def platform_rules(platforms: list[str]) -> str:
    return json.dumps({
        "version": COPY_VERSION,
        "scope": "项目文案交付规范，不代表平台官方全部限制或 API 字符计数规则",
        "common": "每个请求平台恰好一套，无额外平台。每套包含平台名、标题/Hook、正文、CTA、标签。标签必须是至少两个以 # 开头的话题，禁止只写「无」「无标签」「N/A」或「-」。不得缺项或留空。不要输出配图或镜头建议。字数按 Unicode 字符计。",
        "platforms": {p: PLATFORM_RULES.get(p, "未知平台，不能判定通过") for p in platforms},
    }, ensure_ascii=False)


def empty_copy() -> dict:
    return {"version": COPY_VERSION, "platforms": {}}


def canonical_copy(content: dict | str | None) -> str:
    if isinstance(content, str):
        return content
    if not content:
        return ""
    return json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(content: dict | str | None) -> str:
    return hashlib.sha256(canonical_copy(content).encode("utf-8")).hexdigest()


def flatten_copy(content: dict | None) -> str:
    platforms = (content or {}).get("platforms") or {}
    items = platforms if isinstance(platforms, list) else [
        {"platform": key, **value} if isinstance(value, dict) else {"platform": key}
        for key, value in platforms.items()
    ]
    parts = []
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in ("hook", "body", "cta", "tags"):
            value = str(item.get(key) or "").strip()
            if value:
                parts.append(value)
    return "\n".join(parts)


def format_copy(content: dict | None) -> str:
    platforms = (content or {}).get("platforms") or {}
    if isinstance(platforms, list):
        items = [item for item in platforms if isinstance(item, dict)]
    else:
        items = [
            {"platform": key, **value}
            for key, value in platforms.items()
            if isinstance(value, dict)
        ]
    blocks = []
    for item in items:
        platform = item.get("platform") or ""
        blocks.append(
            f"{platform}\n"
            f"Hook: {item.get('hook') or ''}\n"
            f"正文: {item.get('body') or ''}\n"
            f"CTA: {item.get('cta') or ''}\n"
            f"标签: {item.get('tags') or ''}"
        )
    return "\n\n".join(blocks)


def _message_text(raw: str | dict | list) -> str:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        return json.dumps(raw, ensure_ascii=False)
    if isinstance(raw, list):
        parts = []
        for item in raw:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
        return "".join(parts)
    return str(raw)


def parse_copy(raw: str | dict | list | None, platforms: list[str]) -> tuple[dict, list[str]]:
    if isinstance(raw, dict):
        data = raw
    else:
        text = _message_text(raw or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return empty_copy(), ["文案不是合法 JSON"]
    if not isinstance(data, dict):
        return empty_copy(), ["文案 JSON 必须是对象"]

    items = data.get("platforms")
    mapped: dict[str, dict[str, str]] = {}
    seen: list[str] = []
    if isinstance(items, list):
        try:
            generated = GeneratedCopy.model_validate({"platforms": items})
        except ValidationError:
            return empty_copy(), ["平台文案结构无效"]
        for item in generated.platforms:
            key = item.platform.strip().lower()
            seen.append(key)
            mapped[key] = {
                "hook": item.hook,
                "body": item.body,
                "cta": item.cta,
                "tags": item.tags,
            }
    elif isinstance(items, dict):
        for key, item in items.items():
            if not isinstance(item, dict):
                return empty_copy(), ["平台文案必须是对象"]
            platform = str(key).strip().lower()
            seen.append(platform)
            mapped[platform] = {
                "hook": str(item.get("hook") or ""),
                "body": str(item.get("body") or ""),
                "cta": str(item.get("cta") or ""),
                "tags": str(item.get("tags") or ""),
            }
    else:
        return empty_copy(), ["缺少 platforms"]

    dump = {"version": COPY_VERSION, "platforms": mapped}
    errors = []
    if len(seen) != len(set(seen)):
        errors.append("平台审查覆盖不完整、重复或包含未请求的平台")
    errors.extend(validate_copy(dump, platforms))
    return dump, errors


def validate_copy(content: dict | None, platforms: list[str]) -> list[str]:
    errors = []
    try:
        copy = MarketingCopy.model_validate(content or empty_copy())
    except ValidationError:
        return ["文案字段不完整"]
    keys = list(copy.platforms)
    if sorted(keys) != sorted(set(platforms)):
        errors.append("平台审查覆盖不完整、重复或包含未请求的平台")
    if any(platform not in PLATFORM_RULES for platform in platforms):
        errors.append("存在未配置格式规则的平台")
    for platform, item in copy.platforms.items():
        fields = [item.hook, item.body, item.cta, item.tags]
        if any(not field.strip() for field in fields):
            errors.append(f"{platform}：缺少必要字段")
        if platform == "x" and len("\n".join(fields)) > 280:
            errors.append("x：发布文本超过项目的 280 字符预算")
    return errors

"""DeepSeek Flash 单价。每次评测把价卡版本写进快照，避免事后改价把两次成本比歪。"""
from datetime import datetime, timezone

# 官方价卡，2026-09-10 起，deepseek-v4-flash 按 Flash 计费。单位：美元 / 1M tokens。
# 峰时（工作日 UTC 01:00–04:00 与 06:00–10:00）为离峰的两倍。未接入中国节假日日历。
PRICE_CARD_VERSION = "deepseek-flash-2026-09-10"
COST_SCOPE = "deepseek_chat_tokens_for_strategy_and_copy"

OFF_PEAK = {"cache_hit": 0.003, "cache_miss": 0.15, "output": 0.6}
PEAK = {"cache_hit": 0.006, "cache_miss": 0.30, "output": 1.2}


def is_peak(at: datetime) -> bool:
    utc = at.astimezone(timezone.utc) if at.tzinfo else at.replace(tzinfo=timezone.utc)
    if utc.weekday() >= 5:
        return False
    hour = utc.hour
    return (1 <= hour < 4) or (6 <= hour < 10)


def rates_at(at: datetime) -> dict[str, float]:
    return dict(PEAK if is_peak(at) else OFF_PEAK)


def chat_cost_usd(
    input_tokens: int,
    output_tokens: int,
    cache_hit_tokens: int,
    at: datetime,
) -> float:
    rates = rates_at(at)
    hit = max(0, min(cache_hit_tokens, input_tokens))
    miss = max(0, input_tokens - hit)
    usd = (
        hit * rates["cache_hit"]
        + miss * rates["cache_miss"]
        + max(0, output_tokens) * rates["output"]
    ) / 1_000_000
    return round(usd, 8)

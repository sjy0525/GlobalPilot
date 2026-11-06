"""统计一次生成里 DeepSeek 对话的 token，不含向量和重排序。"""
import threading
from datetime import datetime, timezone

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage

from app.modules.evaluation.engine.pricing import chat_cost_usd, is_peak


class UsageHandler(BaseCallbackHandler):
    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_hit_tokens = 0
        self.calls = 0
        self._seen: set[str] = set()
        self._lock = threading.Lock()

    def on_chat_model_end(self, response, *, run_id, **kwargs) -> None:
        self._consume(run_id, response)

    def on_llm_end(self, response, *, run_id, **kwargs) -> None:
        self._consume(run_id, response)

    def _consume(self, run_id, response) -> None:
        key = str(run_id)
        extracted = _extract_usage(response)
        if extracted is None:
            return
        with self._lock:
            if key in self._seen:
                return
            self._seen.add(key)
            incoming, outgoing, cache_hit = extracted
            self.input_tokens += incoming
            self.output_tokens += outgoing
            self.cache_hit_tokens += cache_hit
            self.calls += 1

    def snapshot(self, at: datetime | None = None) -> dict:
        moment = at or datetime.now(timezone.utc)
        with self._lock:
            incoming = self.input_tokens
            outgoing = self.output_tokens
            cache_hit = self.cache_hit_tokens
            calls = self.calls
        return {
            "input_tokens": incoming,
            "output_tokens": outgoing,
            "cache_hit_tokens": cache_hit,
            "calls": calls,
            "peak": is_peak(moment),
            "usd": chat_cost_usd(incoming, outgoing, cache_hit, moment),
        }


def invoke_config(handler: UsageHandler) -> dict:
    return {"callbacks": [handler]}


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _from_mapping(usage: dict) -> tuple[int, int, int] | None:
    incoming = _as_int(
        usage.get("input_tokens")
        if "input_tokens" in usage
        else usage.get("prompt_tokens")
    )
    outgoing = _as_int(
        usage.get("output_tokens")
        if "output_tokens" in usage
        else usage.get("completion_tokens")
    )
    if incoming == 0 and outgoing == 0:
        return None
    details = usage.get("input_token_details") or {}
    cache_hit = _as_int(
        usage.get("prompt_cache_hit_tokens")
        or usage.get("cache_read")
        or (details.get("cache_read") if isinstance(details, dict) else 0)
    )
    return incoming, outgoing, cache_hit


def _extract_usage(response) -> tuple[int, int, int] | None:
    generations = getattr(response, "generations", None) or []
    for group in generations:
        for gen in group:
            message = getattr(gen, "message", None)
            if not isinstance(message, AIMessage):
                continue
            usage = message.usage_metadata or (message.response_metadata or {}).get("token_usage")
            if isinstance(usage, dict):
                parsed = _from_mapping(usage)
                if parsed:
                    return parsed
    llm_output = getattr(response, "llm_output", None) or {}
    token_usage = llm_output.get("token_usage") if isinstance(llm_output, dict) else None
    if isinstance(token_usage, dict):
        return _from_mapping(token_usage)
    return None

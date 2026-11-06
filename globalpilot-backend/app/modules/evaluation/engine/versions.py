"""把一次评测当时的版本钉死，后面改代码也不会改写旧快照。"""
import hashlib
import json

from app.modules.evaluation.engine.cases import TEST_SET_VERSION
from app.modules.evaluation.engine.pricing import COST_SCOPE, PRICE_CARD_VERSION
from app.modules.evaluation.engine.scoring import CRITERIA, LENGTH_LIMITS, SCORING_RULES_VERSION
from app.core.config import settings


def scoring_fingerprint() -> str:
    payload = json.dumps(
        {"criteria": CRITERIA, "lengths": LENGTH_LIMITS},
        ensure_ascii=False,
        sort_keys=True,
        default=list,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def knowledge_version(db, *, team_id) -> str:
    from app.modules.rag.models import KnowledgeDocument

    rows = (
        db.query(
            KnowledgeDocument.id,
            KnowledgeDocument.source,
            KnowledgeDocument.chunk_count,
            KnowledgeDocument.updated_at,
        )
        .filter(KnowledgeDocument.team_id == team_id)
        .order_by(KnowledgeDocument.id)
        .all()
    )
    payload = [
        {
            "id": str(row.id),
            "source": row.source,
            "chunks": row.chunk_count,
            "updated_at": row.updated_at.isoformat() if row.updated_at else "",
        }
        for row in rows
    ]
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:12]
    return f"kb-{len(rows)}-{digest}"


def _template_text(prompt) -> str:
    chunks = []
    for message in getattr(prompt, "messages", []):
        inner = getattr(message, "prompt", None)
        chunks.append(str(getattr(inner, "template", "") or ""))
    return "\n".join(chunks)


def prompt_identity() -> tuple[str, str]:
    from app.modules.marketing.workflow.nodes import PROMPT_VERSION, content_prompt, strategy_prompt
    from app.modules.marketing.workflow.research_agent import RESEARCH_PROMPT, REVIEW_PROMPT

    raw = json.dumps(
        {
            "strategy": _template_text(strategy_prompt),
            "content": _template_text(content_prompt),
            "research": RESEARCH_PROMPT,
            "research_review": REVIEW_PROMPT,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    fingerprint = hashlib.sha256(raw.encode()).hexdigest()[:12]
    return PROMPT_VERSION, fingerprint


def workflow_version() -> str:
    from app.modules.marketing.workflow.graph import WORKFLOW_VERSION

    return WORKFLOW_VERSION


def model_settings() -> dict:
    return {
        "generator": {
            "provider": "deepseek",
            "model": settings.deepseek_model,
            "temperature": 0,
            "base_url": settings.deepseek_base_url,
        },
        "judge": {
            "provider": "deepseek",
            "model": settings.deepseek_model,
            "temperature": 0,
            "role": "independent_scorer",
        },
        "embedding": {
            "model": settings.dashscope_embedding_model,
            "dimensions": settings.dashscope_embedding_dimensions,
        },
        "rerank": {"model": settings.dashscope_rerank_model},
        "rag": {
            "recall_k": settings.rag_recall_k,
            "search_k": settings.rag_search_k,
            "fetch_k": settings.rag_fetch_k,
            "max_distance": settings.rag_max_distance,
        },
        "price_card": PRICE_CARD_VERSION,
        "cost_scope": COST_SCOPE,
        "holiday_calendar": "not_applied",
        "execution_mode": "first_draft_only",
    }


def provenance_snapshot(db, test_set_version: str | None = None, *, team_id) -> dict:
    prompt_version, prompt_fingerprint = prompt_identity()
    return {
        "test_set_version": test_set_version or TEST_SET_VERSION,
        "workflow_version": workflow_version(),
        "prompt_version": prompt_version,
        "prompt_fingerprint": prompt_fingerprint,
        "model_settings": model_settings(),
        "knowledge_version": knowledge_version(db, team_id=team_id),
        "scoring_rules_version": SCORING_RULES_VERSION,
        "scoring_fingerprint": scoring_fingerprint(),
        "criteria": list(CRITERIA),
        "pass_rule": "四项均为通过才算合格；待确认不算通过；超时、空输出和报错计入分母，不能删除。",
        "rate_formula": "初稿合格率 = 未经返工就合格的任务数 ÷ 全部测试任务数",
    }

"""片段分词与 PostgreSQL 全文检索。"""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING
from uuid import UUID

from langchain_core.documents import Document
from sqlalchemy import func, literal_column, select
from starlette.concurrency import run_in_threadpool

if TYPE_CHECKING:
    from langchain_postgres import PGVector

LEXICAL_METADATA_KEY = "lexical_text"
TOKENIZER_VERSION = 1
# 查询和 GIN 索引共用同一表达式。
LEXICAL_INDEX_EXPRESSION = (
    f"to_tsvector('simple'::regconfig, COALESCE(cmetadata ->> '{LEXICAL_METADATA_KEY}'::text, ''::text))"
)
MAX_QUERY_TERMS = 24

_DASHES = str.maketrans("‐‑‒–—−", "------")
_POWER = re.compile(r"(?<![a-z0-9])([0-9]+(?:\.[0-9]+)?)\s*(kw|mw|w|千瓦|瓦)(?![a-z0-9])")
_MODEL = re.compile(r"(?<![a-z0-9])([a-z]+[0-9]+)[\s-]*(pro|max|plus|ultra|mini|air|lite|se)(?![a-z0-9])")
_USB = re.compile(r"(?<![a-z0-9])(usb)[\s-]*([abc])(?![a-z0-9])")
_HYPHENATED = re.compile(r"(?<![a-z0-9])[a-z0-9]+(?:-[a-z0-9]+)+(?![a-z0-9])")
_STOP_WORDS = frozenset(
    "a an and are as at be by can could for from has have how in is it its me of on or "
    "please should that the their them this to was were what when where which who will with would "
    "一个 一下 为什么 什么 可以 哪个 哪里 哪些 多少 如何 怎么 怎样 我们 是否 根据 请问 请告诉 告诉 这个 那个".split()
)


def _power_alias(match: re.Match[str]) -> str:
    number, unit = match.groups()
    unit = {"瓦": "w", "千瓦": "kw"}.get(unit, unit)
    return " " + number.replace(".", "p") + unit + " "


def _identifier_alias(parts: list[str] | tuple[str, ...]) -> str:
    return f" {''.join(parts)} {' '.join(parts)} "


def tokenize(text: str) -> list[str]:
    import jieba

    normalized = unicodedata.normalize("NFKC", text).casefold().translate(_DASHES)
    normalized = _POWER.sub(_power_alias, normalized)
    # 统一带空格、带连字符和连写的型号。
    normalized = _MODEL.sub(lambda match: _identifier_alias(match.groups()), normalized)
    normalized = _USB.sub(lambda match: _identifier_alias(match.groups()), normalized)
    normalized = _HYPHENATED.sub(lambda match: _identifier_alias(match[0].split("-")), normalized)
    return [
        token for token in jieba.cut_for_search(normalized)
        if token.isalnum() and len(token) <= 80
    ]


def lexical_metadata(text: str, metadata: dict) -> dict:
    section = metadata.get("section_path")
    section_fields = section if isinstance(section, (list, tuple)) else [section]
    fields = [
        metadata.get("source"),
        *section_fields,
        metadata.get("caption"),
        metadata.get("context_text"),
        text,
    ]
    terms = tokenize("\n".join(field for field in fields if isinstance(field, str)))
    return {LEXICAL_METADATA_KEY: " ".join(terms), "tokenizer_version": TOKENIZER_VERSION}


def with_lexical_metadata(documents: list[Document]) -> list[Document]:
    return [
        document.model_copy(update={"metadata": {
            **document.metadata, **lexical_metadata(document.page_content, document.metadata),
        }})
        for document in documents
    ]


def _term_priority(term: str) -> tuple[bool, int]:
    is_identifier = term.isascii() and (
        (not term.isalpha() and not term.isdigit()) or term in {"usba", "usbb", "usbc"}
    )
    return is_identifier, len(term)


def keyword_query(question: str) -> str:
    terms = dict.fromkeys(term for term in tokenize(question) if len(term) > 1 and term not in _STOP_WORDS)
    ranked = sorted(terms, key=_term_priority, reverse=True)
    # 词项仅含字母和数字，避免混入查询运算符。
    return " | ".join(ranked[:MAX_QUERY_TERMS])


def _lexical_statement(store: PGVector, collection_id: UUID, query: str, k: int, *, team_id: str, brand_id: str):
    row = store.EmbeddingStore
    vector = literal_column(LEXICAL_INDEX_EXPRESSION)
    tsquery = func.to_tsquery(literal_column("'simple'::regconfig"), query)
    score = func.ts_rank_cd(vector, tsquery).label("keyword_score")
    return (
        select(row.id, row.document, row.cmetadata, score)
        .where(
            row.collection_id == collection_id,
            row.cmetadata["team_id"].astext == team_id,
            row.cmetadata["brand_id"].astext == brand_id,
            vector.bool_op("@@")(tsquery),
        )
        .order_by(score.desc(), row.id)
        .limit(k)
    )


def _as_hits(rows) -> list[tuple[Document, float]]:
    return [
        (Document(id=identifier, page_content=body or "", metadata=metadata or {}), float(score))
        for identifier, body, metadata, score in rows
    ]


def search_lexical(store: PGVector, query: str, k: int, *, team_id: str, brand_id: str) -> list[tuple[Document, float]]:
    terms = keyword_query(query) if k > 0 else ""
    if not terms:
        return []
    with store._make_sync_session() as session:
        collection = store.get_collection(session)
        if collection is None:
            return []
        statement = _lexical_statement(store, collection.uuid, terms, k, team_id=team_id, brand_id=brand_id)
        return _as_hits(session.execute(statement).all())


async def asearch_lexical(
    store: PGVector, query: str, k: int, *, team_id: str, brand_id: str,
) -> list[tuple[Document, float]]:
    terms = await run_in_threadpool(keyword_query, query) if k > 0 else ""
    if not terms:
        return []
    async with store._make_async_session() as session:
        collection = await store.aget_collection(session)
        if collection is None:
            return []
        statement = _lexical_statement(store, collection.uuid, terms, k, team_id=team_id, brand_id=brand_id)
        result = await session.execute(statement)
        return _as_hits(result.all())

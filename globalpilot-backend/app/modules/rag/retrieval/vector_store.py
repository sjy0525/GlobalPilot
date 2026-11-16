import asyncio
import logging
from threading import Lock

from langchain_postgres import PGVector
from sqlalchemy import create_engine, delete, insert, select
from sqlalchemy.engine import Engine
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.db.database import async_engine
from app.infra.ai.embeddings import embeddings

logger = logging.getLogger(__name__)

# 限制单批 SQL 参数量，各批共用同一事务。
DOCUMENT_INSERT_BATCH_SIZE = 64


class _LazyVectorStore:
    """延迟初始化并复用同一个同步向量库实例。"""

    def __init__(self):
        self._store: PGVector | None = None
        self._engine: Engine | None = None
        self._lock = Lock()

    def initialize(self) -> PGVector:
        with self._lock:
            if self._store is None:
                engine = create_engine(settings.database_url)
                try:
                    store = PGVector(
                        embeddings=embeddings,
                        collection_name="brand_knowledge",
                        connection=engine,
                        use_jsonb=True,
                    )
                except BaseException:
                    try:
                        engine.dispose()
                    except Exception:
                        logger.exception("Failed to dispose vector database after initialization failure")
                    raise
                self._engine = engine
                self._store = store
            return self._store

    def close(self) -> None:
        with self._lock:
            if self._engine is not None:
                self._engine.dispose()
                self._engine = None
                self._store = None

    def __getattr__(self, name: str):
        return getattr(self.initialize(), name)


vector_store = _LazyVectorStore()


class _AsyncVectorStore:
    """串行初始化异步向量库，失败后可重试。"""

    def __init__(self):
        self._store: PGVector | None = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> PGVector:
        async with self._lock:
            if self._store is None:
                store = PGVector(
                    embeddings=embeddings,
                    collection_name="brand_knowledge",
                    connection=async_engine,
                    use_jsonb=True,
                    async_mode=True,
                )
                # 初始化成功后再缓存实例。
                await store.__apost_init__()
                self._store = store
            return self._store

    async def close(self) -> None:
        async with self._lock:
            # 共享连接池由应用统一关闭。
            self._store = None

    async def aadd_documents(self, *args, **kwargs):
        store = await self.initialize()
        return await store.aadd_documents(*args, **kwargs)

    async def asimilarity_search_with_score(self, *args, **kwargs):
        store = await self.initialize()
        return await store.asimilarity_search_with_score(*args, **kwargs)


async_vector_store = _AsyncVectorStore()


def initialize_vector_store() -> PGVector:
    return vector_store.initialize()


def close_vector_store() -> None:
    vector_store.close()


async def ainitialize_vector_store() -> PGVector:
    return await async_vector_store.initialize()


async def aclose_vector_store() -> None:
    await async_vector_store.close()


async def get_async_vector_store() -> PGVector:
    return await async_vector_store.initialize()


def brand_filter(brand_id: str, *, team_id: str) -> dict:
    # 单值 $in 使用 ->> 精确匹配，可利用 B-tree 索引。
    return {"brand_id": {"$in": [brand_id]}, "team_id": {"$in": [team_id]}}


def document_filter(brand_id: str, document_id: str, *, team_id: str) -> dict:
    return {
        **brand_filter(brand_id, team_id=team_id),
        "document_id": {"$in": [document_id]},
    }


def chunk_id(brand_id: str, document_id: str, index: int) -> str:
    return f"{brand_id}:{document_id}:{index}"


def _document_chunk_predicates(store, collection_id, brand_id: str, document_id: str, *, team_id: str):
    table = store.EmbeddingStore
    return (
        table.collection_id == collection_id,
        table.cmetadata["team_id"].astext == team_id,
        table.cmetadata["brand_id"].astext == brand_id,
        table.cmetadata["document_id"].astext == document_id,
    )


def _document_chunks_delete(store, collection_id, brand_id: str, document_id: str, *, team_id: str):
    return delete(store.EmbeddingStore).where(
        *_document_chunk_predicates(store, collection_id, brand_id, document_id, team_id=team_id)
    )


def _chunk_rows(collection_id, documents, vectors, ids):
    if not (len(documents) == len(vectors) == len(ids)):
        raise ValueError("Embedding count does not match document chunks")
    return [
        {"id": identifier, "collection_id": collection_id, "embedding": vector,
         "document": document.page_content, "cmetadata": document.metadata}
        for identifier, document, vector in zip(ids, documents, vectors, strict=True)
    ]


def _document_chunk_batches(collection_id, brand_id, document_id, documents, vectors):
    for start in range(0, len(documents), DOCUMENT_INSERT_BATCH_SIZE):
        end = min(start + DOCUMENT_INSERT_BATCH_SIZE, len(documents))
        ids = [chunk_id(brand_id, document_id, index) for index in range(start, end)]
        yield _chunk_rows(collection_id, documents[start:end], vectors[start:end], ids)


def _validate_chunk_scope(documents, *, team_id, brand_id, document_id):
    for document in documents:
        metadata = document.metadata
        if (metadata.get("team_id"), metadata.get("brand_id"), metadata.get("document_id")) != (
            team_id, brand_id, document_id,
        ):
            raise ValueError("Document chunk does not match its team/brand/document scope")


def write_document_chunks(db, store, brand_id: str, document_id: str, documents, vectors, *, team_id: str) -> int:
    """锁定文档行后，在调用方事务内替换片段。"""
    _validate_chunk_scope(documents, team_id=team_id, brand_id=brand_id, document_id=document_id)
    collection = store.get_collection(db)
    if collection is None:
        raise RuntimeError("Knowledge collection is not initialized")
    if len(documents) != len(vectors):
        raise ValueError("Embedding count does not match document chunks")
    result = db.execute(_document_chunks_delete(store, collection.uuid, brand_id, document_id, team_id=team_id))
    for rows in _document_chunk_batches(collection.uuid, brand_id, document_id, documents, vectors):
        db.execute(insert(store.EmbeddingStore).values(rows))
    return result.rowcount


async def awrite_document_chunks(db, store, brand_id: str, document_id: str, documents, vectors, *, team_id: str) -> int:
    """在调用方事务内异步替换片段。"""
    _validate_chunk_scope(documents, team_id=team_id, brand_id=brand_id, document_id=document_id)
    collection = await store.aget_collection(db)
    if collection is None:
        raise RuntimeError("Knowledge collection is not initialized")
    if len(documents) != len(vectors):
        raise ValueError("Embedding count does not match document chunks")
    result = await db.execute(_document_chunks_delete(store, collection.uuid, brand_id, document_id, team_id=team_id))
    for rows in _document_chunk_batches(collection.uuid, brand_id, document_id, documents, vectors):
        await db.execute(insert(store.EmbeddingStore).values(rows))
    return result.rowcount


def delete_by_metadata(metadata_filter: dict, *, team_id: str) -> int:
    with vector_store._make_sync_session() as session:
        collection = vector_store.get_collection(session)
        if collection is None:
            return 0

        clause = vector_store._create_filter_clause({"$and": [metadata_filter, {"team_id": {"$in": [team_id]}}]})
        rows = session.execute(
            select(vector_store.EmbeddingStore.id).where(
                vector_store.EmbeddingStore.collection_id == collection.uuid,
                clause,
            )
        ).all()
        ids = [row[0] for row in rows]

    if ids:
        vector_store.delete(ids=ids, collection_only=True)
    return len(ids)


def _document_text_statement(store, collection_id, brand_id: str, document_id: str, *, team_id: str):
    return select(store.EmbeddingStore.document, store.EmbeddingStore.cmetadata).where(
        *_document_chunk_predicates(store, collection_id, brand_id, document_id, team_id=team_id)
    )


def load_document_text(brand_id: str, document_id: str, *, team_id: str, db=None, store=None) -> str:
    store = store or vector_store.initialize()

    def read_rows(session):
        collection = store.get_collection(session)
        if collection is None:
            return []
        return session.execute(_document_text_statement(store, collection.uuid, brand_id, document_id, team_id=team_id)).all()

    if db is None:
        with store._make_sync_session() as session:
            rows = read_rows(session)
    else:
        # 预览复用同一事务读取文档元数据和片段。
        rows = read_rows(db)
    return _join_document_rows(rows)


def _join_document_rows(rows) -> str:
    parts: list[tuple[int, str]] = []
    for document, metadata in rows:
        index = 0
        if isinstance(metadata, dict):
            try:
                index = int(metadata.get("chunk_index") or 0)
            except (TypeError, ValueError):
                index = 0
        text = (document or "").strip()
        if text:
            parts.append((index, text))
    parts.sort(key=lambda item: item[0])
    return "\n\n".join(text for _, text in parts)


async def adelete_by_metadata(metadata_filter: dict, *, team_id: str) -> int:
    store = await get_async_vector_store()
    async with store._make_async_session() as session:
        collection = await store.aget_collection(session)
        if collection is None:
            return 0
        clause = store._create_filter_clause({"$and": [metadata_filter, {"team_id": {"$in": [team_id]}}]})
        result = await session.execute(
            select(store.EmbeddingStore.id).where(
                store.EmbeddingStore.collection_id == collection.uuid,
                clause,
            )
        )
        ids = [row[0] for row in result.all()]
    if ids:
        await store.adelete(ids=ids, collection_only=True)
    return len(ids)


async def aload_document_text(brand_id: str, document_id: str, *, team_id: str, db=None, store=None) -> str:
    store = store or await get_async_vector_store()

    async def read_rows(session):
        collection = await store.aget_collection(session)
        if collection is None:
            return []
        result = await session.execute(_document_text_statement(store, collection.uuid, brand_id, document_id, team_id=team_id))
        return result.all()

    if db is None:
        async with store._make_async_session() as session:
            rows = await read_rows(session)
    else:
        rows = await read_rows(db)
    return await run_in_threadpool(_join_document_rows, rows)

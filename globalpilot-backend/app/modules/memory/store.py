#管理长记忆存储

from contextlib import contextmanager
from collections.abc import Iterator
from uuid import UUID

from langgraph.store.postgres import PostgresStore

from app.core.config import settings
from app.infra.ai.embeddings import embeddings
from app.modules.memory.schemas.brand_memory import BrandMemory, MemoryScope
from app.modules.marketing.workflow.execution import to_psycopg_conninfo

def memory_namespace(scope:MemoryScope)->tuple[str, ...]:
    return (
        "brand_preferences_v2",
        str(scope.team_id),
        str(scope.user_id),
        str(scope.brand_profile_id)
    )

@contextmanager
def open_memory_store()->Iterator[PostgresStore]:
    with PostgresStore.from_conn_string(
        to_psycopg_conninfo(settings.database_url),
        index={
            "dims":settings.dashscope_embedding_dimensions,
            "embed":embeddings,
            "fields":["content"],
        }
    )as store:
        yield store

def save_memory(
    store: PostgresStore,
    scope: MemoryScope,
    memory: BrandMemory,
) -> None:
    if memory.duration != "long_term":
        raise ValueError("临时要求不能写入长期记忆")

    store.put(
        memory_namespace(scope),
        str(memory.id),
        memory.model_dump(mode="json"),
    )


def get_memory(
    store: PostgresStore,
    scope: MemoryScope,
    memory_id: UUID,
) -> BrandMemory | None:
    item = store.get(
        memory_namespace(scope),
        str(memory_id),
    )

    if item is None:
        return None

    return BrandMemory.model_validate(item.value)


def search_memories(
    store: PostgresStore,
    scope: MemoryScope,
    query: str,
) -> list[BrandMemory]:
    items = store.search(
        memory_namespace(scope),
        query=query,
        filter={
            "status": "active",
            "duration": "long_term",
        },
        limit=5,
    )

    return [
        BrandMemory.model_validate(item.value)
        for item in items
    ]

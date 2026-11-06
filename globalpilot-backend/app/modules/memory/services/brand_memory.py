"""在同一 PostgreSQL 事务内更新品牌偏好并保存旧版快照。"""

import hashlib
from datetime import datetime, timezone
from uuid import UUID

from psycopg import Connection

from app.modules.memory.schemas.brand_memory import (
    BrandMemory,
    MemoryScope,
    MemorySource,
    UpdateMemoryArgs,
    InvalidateMemoryArgs,
)
from app.modules.memory.store import (
    get_memory,
    memory_namespace,
    open_memory_store,
    save_memory,
)


class MemoryUpdateError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def memory_lock_key(scope: MemoryScope) -> int:
    """同一团队、用户和品牌在不同进程中使用相同的事务锁。"""

    identity = "\0".join(memory_namespace(scope)).encode()
    digest = hashlib.blake2b(
        b"globalpilot:brand-memory:" + identity,
        digest_size=8,
    ).digest()
    return int.from_bytes(digest, "big", signed=True)


def history_namespace(
    scope: MemoryScope,
    memory_id: UUID,
) -> tuple[str, ...]:
    return (
        "brand_preference_history_v2",
        str(scope.team_id),
        str(scope.user_id),
        str(scope.brand_profile_id),
        str(memory_id),
    )

#更新记忆
def update_brand_memory(
    scope: MemoryScope,
    source: MemorySource,
    args: UpdateMemoryArgs,
) -> BrandMemory:
    """更新调用方已授权范围内的有效长期记忆，版本冲突时拒绝写入。"""

    scope = MemoryScope.model_validate(scope.model_dump())
    source = MemorySource.model_validate(source.model_dump())
    args = UpdateMemoryArgs.model_validate(args.model_dump())
    if not source.quote.strip() or not source.checkpoint_id.strip():
        raise MemoryUpdateError(
            "invalid_source",
            "用户原话和来源 checkpoint 不能为空",
        )

    with open_memory_store() as store:
        if not isinstance(store.conn, Connection):
            raise RuntimeError("更新记忆需要独立的 PostgreSQL 连接")

        with store.conn.transaction():
            with store.conn.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (memory_lock_key(scope),),
                )

            old = get_memory(store, scope, args.memory_id)
            if old is None:
                raise MemoryUpdateError(
                    "not_found", "当前品牌下未找到这条记忆",
                )
            if old.status != "active" or old.duration != "long_term":
                raise MemoryUpdateError(
                    "not_editable", "只能更新有效的长期记忆",
                )
            if old.version != args.expected_version:
                raise MemoryUpdateError(
                    "version_conflict", "记忆已变化，请重新搜索后判断",
                )

            payload = old.model_dump(mode="json")
            payload.update(args.model_dump(
                exclude={"memory_id", "expected_version"},
                exclude_none=True,
            ))
            payload["version"] = old.version + 1
            payload["update_at"] = datetime.now(timezone.utc)
            source_data = source.model_dump(mode="json")
            if source_data not in payload["sources"]:
                payload["sources"].append(source_data)
            updated = BrandMemory.model_validate(payload)

            history_ns = history_namespace(scope, old.id)
            history_key = str(old.version)
            if store.get(history_ns, history_key) is not None:
                raise MemoryUpdateError(
                    "history_conflict", "旧版快照已存在，请检查记忆状态",
                )
            store.put(
                history_ns,
                history_key,
                old.model_dump(mode="json"),
                index=False,
            )
            save_memory(store, scope, updated)

    return updated


# 使记忆失效，并保留来源和旧版快照。
def invalidate_brand_memory(
    scope: MemoryScope,
    source: MemorySource,
    args: InvalidateMemoryArgs,
) -> BrandMemory:
    """在已授权范围内失效一条长期偏好，重复或过期版本不会覆盖当前状态。"""

    scope = MemoryScope.model_validate(scope.model_dump())
    source = MemorySource.model_validate(source.model_dump())
    args = InvalidateMemoryArgs.model_validate(args.model_dump())

    if not source.quote.strip() or not source.checkpoint_id.strip():
        raise MemoryUpdateError(
            "invalid_source",
            "用户原话和来源 checkpoint 不能为空",
        )

    with open_memory_store() as store:
        if not isinstance(store.conn, Connection):
            raise RuntimeError("失效操作需要独立的 PostgreSQL 连接")

        with store.conn.transaction():
            with store.conn.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (memory_lock_key(scope),),
                )

            old = get_memory(store, scope, args.memory_id)
            if old is None:
                raise MemoryUpdateError(
                    "not_found", "当前品牌下未找到这条记忆",
                )
            if old.version != args.expected_version:
                raise MemoryUpdateError(
                    "version_conflict", "记忆已变化，请重新搜索后判断",
                )
            if old.status != "active" or old.duration != "long_term":
                raise MemoryUpdateError(
                    "not_editable", "只能失效有效的长期记忆",
                )

            payload = old.model_dump(mode="json")
            payload.update(
                status="inactive",
                version=old.version + 1,
                update_at=datetime.now(timezone.utc),
                invalidation_reason=args.reason,
            )
            source_data = source.model_dump(mode="json")
            if source_data not in payload["sources"]:
                payload["sources"].append(source_data)
            invalidated = BrandMemory.model_validate(payload)

            history_ns = history_namespace(scope, old.id)
            history_key = str(old.version)
            if store.get(history_ns, history_key) is not None:
                raise MemoryUpdateError(
                    "history_conflict", "旧版快照已存在，请检查记忆状态",
                )
            store.put(
                history_ns,
                history_key,
                old.model_dump(mode="json"),
                index=False,
            )

            # 正文未改；保留已有向量，召回通过 active 状态过滤。
            store.put(
                memory_namespace(scope),
                str(invalidated.id),
                invalidated.model_dump(mode="json"),
                index=False,
            )

    return invalidated

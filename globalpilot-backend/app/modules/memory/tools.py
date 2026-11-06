# 封装品牌偏好的创建、搜索、更新和失效工具。

from uuid import UUID

from langchain_core.tools import BaseTool, tool

from app.modules.memory.schemas.brand_memory import (
    BrandMemory,
    MemoryScope,
    MemorySource,
    CreateMemoryArgs,
    SearchMemoryArgs,
    UpdateMemoryArgs,
    InvalidateMemoryArgs,
)
from app.modules.memory.services.brand_memory import (
    MemoryUpdateError,
    update_brand_memory,
    invalidate_brand_memory,
)
from app.modules.memory.store import (
    open_memory_store,
    save_memory,
    search_memories,
)

def build_create_memory_tool(
    scope:MemoryScope,
    source:MemorySource
):
    build_scope=scope.model_copy(deep=True)
    build_source=source.model_copy(deep=True)

    if not build_source.quote.strip():
        raise ValueError("用户原话不能为空")
    if not build_source.checkpoint_id.strip():
        raise ValueError("来源任务不能为空")

    
    @tool("create_memory",args_schema=CreateMemoryArgs)
    def create_memory(
        content:str,
        regions:str,
        platforms:list[str]|None=None,
    )->dict:
        """保存明确的长期品牌偏好；临时任务要求不应调用此工具。"""
        memory=BrandMemory(
            content=content,
            regions=regions,
            platforms=platforms or [],
            sources=[build_source.model_copy(deep=True)],
            duration="long_term",
            status="active",
            version=1,
        )

        with open_memory_store() as store:
            save_memory(store,build_scope,memory)

        return{
            "memory_id":str(memory.id),
            "version":memory.version
        }

    
    return create_memory


def build_search_memory_tool(scope: MemoryScope) -> BaseTool:
    bound_scope = scope.model_copy(deep=True)

    @tool("search_memory", args_schema=SearchMemoryArgs)
    def search_memory(query: str) -> dict:
        """搜索当前用户、当前品牌的有效长期偏好，供新增或更新记忆前参考。"""

        with open_memory_store() as store:
            memories = search_memories(store, bound_scope, query)

        return {
            "memories": [
                {
                    "memory_id": str(memory.id),
                    "version": memory.version,
                    "content": memory.content,
                    "regions": memory.regions,
                    "platforms": memory.platforms,
                    "sources": [
                        source.model_dump(mode="json")
                        for source in memory.sources
                    ],
                }
                for memory in memories
            ]
        }

    return search_memory


def build_update_memory_tool(
    scope: MemoryScope,
    source: MemorySource,
) -> BaseTool:
    bound_scope = scope.model_copy(deep=True)
    bound_source = source.model_copy(deep=True)

    @tool("update_memory", args_schema=UpdateMemoryArgs)
    def update_memory(
        memory_id: UUID,
        expected_version: int,
        content: str,
        regions: str | None = None,
        platforms: list[str] | None = None,
    ) -> dict:
        """更新已有长期偏好。先搜索取得 ID 和版本；版本冲突后重新搜索并判断。"""

        args = UpdateMemoryArgs(
            memory_id=memory_id,
            expected_version=expected_version,
            content=content,
            regions=regions,
            platforms=platforms,
        )
        try:
            updated = update_brand_memory(bound_scope, bound_source, args)
        except MemoryUpdateError as exc:
            return {"ok": False, "error": exc.code, "message": str(exc)}
        return {"ok": True, "memory_id": str(updated.id), "version": updated.version}

    return update_memory


def build_invalidate_memory_tool(
    scope: MemoryScope,
    source: MemorySource,
) -> BaseTool:
    bound_scope = scope.model_copy(deep=True)
    bound_source = source.model_copy(deep=True)

    @tool("invalidate_memory", args_schema=InvalidateMemoryArgs)
    def invalidate_memory(
        memory_id: UUID,
        expected_version: int,
        reason: str,
    ) -> dict:
        """将已撤回或过时的长期偏好标记失效；临时任务例外不应调用。"""

        args = InvalidateMemoryArgs(
            memory_id=memory_id,
            expected_version=expected_version,
            reason=reason,
        )
        try:
            invalidated = invalidate_brand_memory(bound_scope, bound_source, args)
        except MemoryUpdateError as exc:
            return {"ok": False, "error": exc.code, "message": str(exc)}
        return {
            "ok": True,
            "memory_id": str(invalidated.id),
            "version": invalidated.version,
            "status": invalidated.status,
        }

    return invalidate_memory

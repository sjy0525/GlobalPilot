from collections.abc import Iterator, Sequence
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import ChannelVersions, Checkpoint, CheckpointMetadata, CheckpointTuple
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.core.config import settings
from app.modules.marketing.workflow.execution import (
    current_workflow_execution,
    require_workflow_execution,
    to_psycopg_conninfo,
)


class FencedPostgresSaver(PostgresSaver):
    """Use the execution session when owned; allow only reads through the pool."""

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        guard = current_workflow_execution(config["configurable"]["thread_id"])
        if guard is None:
            return super().get_tuple(config)
        with guard.checkpoint_saver(self.serde) as saver:
            return saver.get_tuple(config)

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        guard = (
            current_workflow_execution(config["configurable"]["thread_id"])
            if config is not None else None
        )
        if guard is None:
            yield from super().list(config, filter=filter, before=before, limit=limit)
        else:
            with guard.checkpoint_saver(self.serde) as saver:
                yield from saver.list(config, filter=filter, before=before, limit=limit)

    def get_delta_channel_history(self, *, config: RunnableConfig, channels: Sequence[str]):
        guard = current_workflow_execution(config["configurable"]["thread_id"])
        if guard is None:
            return super().get_delta_channel_history(config=config, channels=channels)
        with guard.checkpoint_saver(self.serde) as saver:
            return saver.get_delta_channel_history(config=config, channels=channels)

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        guard = require_workflow_execution(config["configurable"]["thread_id"])
        with guard.checkpoint_saver(self.serde) as saver:
            return saver.put(config, checkpoint, metadata, new_versions)

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        guard = require_workflow_execution(config["configurable"]["thread_id"])
        with guard.checkpoint_saver(self.serde) as saver:
            saver.put_writes(config, writes, task_id, task_path)

    def delete_thread(self, thread_id: str) -> None:
        guard = require_workflow_execution(thread_id)
        with guard.checkpoint_saver(self.serde) as saver:
            saver.delete_thread(thread_id)

    def copy_thread(self, source_thread_id: str, target_thread_id: str) -> None:
        raise NotImplementedError("Workflow checkpoint copying requires a separate ownership protocol")

    def delete_for_runs(self, run_ids: Sequence[str]) -> None:
        raise NotImplementedError("Workflow checkpoint deletion must hold the thread execution lock")

    def prune(self, thread_ids: Sequence[str], *, strategy: str = "keep_latest") -> None:
        raise NotImplementedError("Workflow checkpoint pruning must hold the thread execution lock")


pool = ConnectionPool(
    conninfo=to_psycopg_conninfo(settings.database_url),
    min_size=1,
    max_size=10,
    open=False,
    kwargs={
        "autocommit": True,
        "prepare_threshold": 0,
        "row_factory": dict_row,
    },
)
checkpointer = FencedPostgresSaver(pool)


def setup_checkpointer() -> None:
    pool.open()
    checkpointer.setup()


def close_checkpointer() -> None:
    pool.close()

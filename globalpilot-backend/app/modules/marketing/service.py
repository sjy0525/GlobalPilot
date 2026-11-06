import json
from collections.abc import Iterator
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from langgraph.types import Command
from psycopg.types.json import Jsonb

from app.core.http_errors import stream_error_event
from app.modules.marketing.schemas.copy import MarketingCopy
from app.modules.marketing.workflow.self_review import review_passed
from app.modules.briefs.authorization import (
    load_owned_team_brief_cursor, require_owned_team_brief,
)
from app.modules.marketing.schemas.marketing_workflow import MarketingWorkflowMiddleOutput, MarketingWorkflowReviewInput
from app.modules.marketing.workflow.execution import (
    preserve_execution_context,
    require_workflow_execution,
    workflow_execution,
)
from app.modules.marketing.workflow.graph import (
    MAX_SELF_REVIEW_ATTEMPTS,
    graph,
    revisions_exhausted,
)
from app.modules.marketing.workflow.nodes import (
    MAX_REVISION_PREFIX,
    NO_KNOWLEDGE,
    SELF_REVIEW_EXHAUSTED_PREFIX,
)

HIDDEN_STREAM_KEYS = {"__interrupt__", "__metadata__", "__end__"}


def next_node_after(node: str, update: dict[str, Any] | None) -> str | None:
    payload = update or {}
    if node == "retrieve_context":
        return "research_agent" if payload.get("context") else "no_context"
    if node == "research_agent":
        return "generate_strategy"
    if node == "generate_strategy":
        return "generate_content"
    if node == "generate_content":
        return "self_review"
    if node == "self_review":
        report = payload.get("self_review") or {}
        attempts = int(payload.get("self_review_attempts") or 0)
        if report.get("status") == "passed":
            return "human_review"
        if attempts >= MAX_SELF_REVIEW_ATTEMPTS:
            return "self_review_exhausted"
        return "generate_content"
    if node == "human_review":
        decision = payload.get("decision")
        revision_count = payload.get("revision_count") or 0
        if decision == "approved":
            return "final_output"
        if decision == "rejected":
            if revisions_exhausted(revision_count):
                return "max_revisions"
            return "generate_strategy"
    return None


def node_event(node: str, phase: str, update: dict[str, Any] | None = None) -> dict[str, Any]:
    event: dict[str, Any] = {"type": "node", "node": node, "phase": phase}
    if phase == "done" and isinstance(update, dict):
        strategy = update.get("strategy")
        content = update.get("content")
        if strategy:
            event["strategy"] = strategy
        if content:
            event["content"] = content
        if "self_review" in update:
            event["self_review"] = update["self_review"]
        for key in ("research", "research_validation"):
            if key in update:
                event[key] = update[key]
    return event


def encode_stream_event(event: dict[str, Any]) -> bytes:
    payload = dict(event)
    workflow = payload.get("workflow")
    if isinstance(workflow, MarketingWorkflowMiddleOutput):
        payload["workflow"] = workflow.model_dump(mode="json")
    line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    # Keep proxies from buffering tiny NDJSON frames.
    return line + (b"{%b}\n" % (b" " * 1024))


def iter_graph_events(
    payload: Any, config: dict, first_node: str | None, *, authorize=None,
) -> Iterator[dict[str, Any]]:
    if authorize is not None:
        authorize()
    if first_node:
        yield node_event(first_node, "running")
    for chunk in graph.stream(payload, config, stream_mode="updates"):
        if authorize is not None:
            authorize()
        if not isinstance(chunk, dict):
            continue
        for node_name, update in chunk.items():
            if node_name in HIDDEN_STREAM_KEYS or str(node_name).startswith("__"):
                continue
            update_dict = update if isinstance(update, dict) else {}
            yield node_event(str(node_name), "done", update_dict)
            nxt = next_node_after(str(node_name), update_dict)
            if nxt:
                yield node_event(nxt, "running")


def get_owned_brief(db, user, brief_id, *, team_id):
    return require_owned_team_brief(
        db, team_id=team_id, actor_user_id=user.id, brief_id=brief_id,
    )


def _validate_snapshot_scope(brief, snapshot):
    # Old personal/global heads can contain unscoped retrieved context. Assigning
    # their Brief to a team must not silently expose or resume that old context.
    values = snapshot.values or {}
    if not values:
        return
    for key, expected in (("team_id", brief.team_id), ("actor_user_id", brief.user_id)):
        if str(values.get(key)) != str(expected):
            raise HTTPException(409, "流程团队范围不一致，请联系管理员核对历史数据")


#讲brief_id转为Langgraph的配置
def graph_config(brief):
    return {
        "configurable":{
            "thread_id":str(brief.id)
        }
    }

#读取当前状态
def load_workflow(db,user,brief_id, *, team_id):
    brief=get_owned_brief(db,user,brief_id, team_id=team_id)
    config=graph_config(brief)
    snapshot=graph.get_state(config)
    _validate_snapshot_scope(brief, snapshot)
    get_owned_brief(db, user, brief_id, team_id=team_id)
    return brief,config,snapshot

#最后输出
def to_response(brief,snapshot)->MarketingWorkflowMiddleOutput:
    values=snapshot.values or {}
    final_output=values.get("final_output") or None
    strategy=values.get("strategy") or None
    content=values.get("content") or None

    if pending_human_review(snapshot):
        status="awaiting_review"
    elif final_output==NO_KNOWLEDGE:
        status="no_knowledge"
    elif final_output and final_output.startswith(MAX_REVISION_PREFIX):
        status="max_revisions"
    elif final_output and final_output.startswith(SELF_REVIEW_EXHAUSTED_PREFIX):
        status="self_review_failed"
    elif final_output:
        status="completed"
    else:
        raise HTTPException(status_code=409,detail="当前流程正在处理中，请稍后恢复")

    # Only expose a passed report at human review; failed drafts auto-revise in-graph.
    # Exhausted runs keep the last failure report for debugging.
    report = values.get("self_review")
    if status == "awaiting_review" and not review_passed(values):
        report = None

    return MarketingWorkflowMiddleOutput(
        brief_id=brief.id,
        checkpoint_id=snapshot_checkpoint_id(snapshot),
        status=status,
        revision_count=values.get("revision_count") or 0,
        self_review=report,
        research=values.get("research") or None,
        research_validation=values.get("research_validation") or None,
        strategy=strategy,
        content=content,
        final_output=final_output,
    )


def dump_content(content: MarketingCopy | dict | None) -> dict | None:
    if content is None:
        return None
    if isinstance(content, MarketingCopy):
        return content.model_dump(mode="json")
    return content


def snapshot_checkpoint_id(snapshot) -> str | None:
    config = getattr(snapshot, "config", None) or {}
    return config.get("configurable", {}).get("checkpoint_id")


def pending_human_review(snapshot) -> bool:
    # A persisted interrupt can remain after its task's result was saved but
    # before the next checkpoint was committed. That decision already took
    # effect; a new Command(resume=...) would be ignored by LangGraph.
    tasks = getattr(snapshot, "tasks", ())
    if tasks:
        return any(
            task.name == "human_review" and task.interrupts and task.result is None
            for task in tasks
        )
    return bool(snapshot.interrupts)


def _load_execution_brief(guard, user_id, *, team_id):
    # Capture only plain values; no ORM transaction stays open during model IO.
    with guard.transaction() as cursor:
        row = load_owned_team_brief_cursor(
            cursor, team_id=team_id, actor_user_id=user_id,
            brief_id=guard.brief_id,
        )
    return SimpleNamespace(**row)


def _initial_payload(brief):
    return {key: getattr(brief, key) for key in (
        "brand_id", "goal", "selling_points", "region", "user_persona",
        "social_media", "purchase_channels", "competitor_url",
    )} | {"brief_id": brief.id, "team_id": brief.team_id, "actor_user_id": brief.user_id}


def persist_snapshot(brief, snapshot) -> MarketingWorkflowMiddleOutput:
    """Publish only the current head, on the connection owning its execution lock."""
    guard = require_workflow_execution(str(brief.id))
    latest = graph.get_state(graph_config(brief))
    _validate_snapshot_scope(brief, latest)
    checkpoint = snapshot_checkpoint_id(snapshot)
    if not checkpoint or checkpoint != snapshot_checkpoint_id(latest):
        raise HTTPException(409, "流程已更新，旧结果不能覆盖当前状态")
    response = to_response(brief, latest)
    with guard.transaction() as cursor:
        # Serialize publication with membership removal, but never hold this
        # lock while awaiting an external model or reading checkpoints.
        cursor.execute("SELECT id FROM teams WHERE id = %s FOR UPDATE", (brief.team_id,))
        load_owned_team_brief_cursor(
            cursor, team_id=brief.team_id, actor_user_id=brief.user_id, brief_id=brief.id,
        )
        cursor.execute(
            "SELECT workflow_version FROM briefs WHERE id = %s FOR UPDATE",
            (brief.id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise HTTPException(404, "营销任务不存在")
        if row["workflow_version"] != brief.workflow_version:
            raise HTTPException(409, "流程已更新，旧结果不能覆盖当前状态")
        cursor.execute(
            """UPDATE briefs SET workflow_status = %s, strategy = %s,
                   content = %s, final_output = %s, revision_count = %s,
                   workflow_checkpoint_id = %s,
                   workflow_version = workflow_version + 1, updated_at = %s
               WHERE id = %s AND workflow_version = %s RETURNING workflow_version""",
            (response.status, response.strategy, Jsonb(dump_content(response.content)),
             response.final_output, response.revision_count, checkpoint, datetime.now(),
             brief.id, brief.workflow_version),
        )
        updated = cursor.fetchone()
        if updated is None:
            raise HTTPException(409, "流程已更新，旧结果不能覆盖当前状态")
        events = (latest.values or {}).get("accepted_memory_events") or []
        if events:
            from app.modules.memory.services.memory_job import enqueue_memory_feedback_events

            enqueue_memory_feedback_events(cursor, brief, events)
    brief.workflow_version = updated["workflow_version"]
    return response


def validate_approval(snapshot, action):
    if action == "approved" and not review_passed(snapshot.values or {}):
        raise HTTPException(status_code=409, detail="文案尚未通过模型自审，请先退回修改")


def validate_review(snapshot, payload):
    if snapshot_checkpoint_id(snapshot) != payload.expected_checkpoint_id:
        raise HTTPException(409, "稿件已更新，请刷新后重新审核")
    if not pending_human_review(snapshot):
        raise HTTPException(409, "当前不在待审流程")
    validate_approval(snapshot, payload.action)


def _start_input(brief, snapshot):
    values = snapshot.values or {}
    if pending_human_review(snapshot) or values.get("final_output"):
        return False, None
    # Resume interrupted execution after process loss; never reset an existing head.
    return True, None if snapshot_checkpoint_id(snapshot) else _initial_payload(brief)


def start(user_id: UUID, brief_id: UUID, *, team_id: UUID) -> MarketingWorkflowMiddleOutput:
    with workflow_execution(brief_id) as guard:
        brief = _load_execution_brief(guard, user_id, team_id=team_id)
        config = graph_config(brief)
        snapshot = graph.get_state(config)
        _validate_snapshot_scope(brief, snapshot)
        should_run, graph_input = _start_input(brief, snapshot)
        if should_run:
            graph.invoke(graph_input, config)
        return persist_snapshot(brief, graph.get_state(config))


def review(user_id: UUID, payload: MarketingWorkflowReviewInput, *, team_id: UUID) -> MarketingWorkflowMiddleOutput:
    with workflow_execution(payload.brief_id) as guard:
        brief = _load_execution_brief(guard, user_id, team_id=team_id)
        config = graph_config(brief)
        snapshot = graph.get_state(config)
        _validate_snapshot_scope(brief, snapshot)
        validate_review(snapshot, payload)
        graph.invoke(Command(resume={
            "action": payload.action, "feedback": payload.feedback or "",
            "source_checkpoint_id": payload.expected_checkpoint_id,
        }), config)
        return persist_snapshot(brief, graph.get_state(config))


def get_workflow(db, user, brief_id, *, team_id: UUID) -> MarketingWorkflowMiddleOutput:
    brief = get_owned_brief(db, user, brief_id, team_id=team_id)
    snapshot = graph.get_state(graph_config(brief))
    _validate_snapshot_scope(brief, snapshot)
    get_owned_brief(db, user, brief_id, team_id=team_id)
    if not snapshot.values:
        raise HTTPException(404, "当前流程尚未生成")
    # Content, review and client token come from exactly the same checkpoint.
    # A failed projection commit can no longer make GET return a different draft.
    return to_response(brief, snapshot)


def review_targeted_parent_content(state: dict) -> dict:
    """Audit the complete resulting copy once, without regenerating any field."""
    from app.modules.marketing.workflow.self_review import self_review

    return self_review(state)


def apply_targeted_revision_to_parent(
    guard,
    brief,
    new_content: dict,
    *,
    expected_checkpoint_id: str,
    revision_task_id: UUID,
    before_publish=None,
    revision_review_context=None,
) -> dict:
    """Adopt a reviewed paragraph result into the existing marketing workflow.

    Checkpoint and Brief body publication share the lock connection and a short
    transaction. The human-review interrupt is materialized only after releasing
    the lock's RLock, so LangGraph's background checkpoint writer can proceed.
    The task marker supports recovery if execution stops between these stages.
    """
    from copy import deepcopy

    from app.modules.marketing.workflow.copy import content_hash, validate_copy

    require_workflow_execution(brief.id)
    config = graph_config(brief)
    current = graph.get_state(config)
    _validate_snapshot_scope(brief, current)
    values = current.values or {}
    marker = str(revision_task_id)
    already_published = (
        values.get("targeted_revision_task_id") == marker
        and content_hash(values.get("content")) == content_hash(new_content)
    )
    if not already_published and snapshot_checkpoint_id(current) != expected_checkpoint_id:
        raise HTTPException(409, "营销原稿已更新，请重新创建改稿任务")

    if not already_published:
        format_issues = validate_copy(new_content, list(brief.social_media))
        if format_issues:
            raise HTTPException(409, {"message": "改稿不符合完整文案格式", "issues": format_issues})
        review_state = deepcopy(values)
        review_state.update({
            "content": new_content, "self_review": None, "self_review_attempts": 0,
        })
        if revision_review_context is not None:
            from app.modules.marketing.schemas.revision import RevisionReviewContext

            evidence_context = RevisionReviewContext.model_validate(
                revision_review_context.model_dump(warnings=False)
                if isinstance(revision_review_context, RevisionReviewContext)
                else revision_review_context
            )
            # Only authorized source excerpts become fact context. Task rules
            # and human clarification text are never promoted to brand facts.
            evidence_text = "\n\n本次定向改稿采用的品牌证据（固定来源和版本）：\n" + json.dumps(
                [item.model_dump(mode="json") for item in evidence_context.evidence],
                ensure_ascii=False,
            )
            base_context = review_state.get("retrieved_context") or review_state.get("context") or ""
            review_state["retrieved_context"] = base_context + evidence_text
            review_state["context"] = base_context + evidence_text
        checked = review_targeted_parent_content(review_state)
        review_state.update(checked)
        if not review_passed(review_state):
            raise HTTPException(409, {
                "message": "改稿未通过完整营销文案检查，未覆盖原稿",
                "review": checked.get("self_review"),
            })

        with guard.transaction() as cursor:
            cursor.execute("SELECT id FROM teams WHERE id = %s FOR UPDATE", (brief.team_id,))
            load_owned_team_brief_cursor(
                cursor, team_id=brief.team_id, actor_user_id=brief.user_id, brief_id=brief.id,
            )
            cursor.execute("SELECT * FROM briefs WHERE id = %s FOR UPDATE", (brief.id,))
            row = cursor.fetchone()
            if row is None:
                raise HTTPException(404, "营销任务不存在")
            latest = graph.get_state(config)
            if snapshot_checkpoint_id(latest) != expected_checkpoint_id:
                raise HTTPException(409, "营销原稿已更新，请重新创建改稿任务")
            if before_publish is not None:
                before_publish()
            updated_config = graph.update_state(config, {
                "content": new_content,
                "self_review": checked["self_review"],
                "self_review_attempts": 0,
                "final_output": "",
                "human_feedback": None,
                "targeted_revision_task_id": marker,
                "retrieved_context": review_state.get("retrieved_context", ""),
                "context": review_state.get("context", ""),
            }, as_node="self_review")
            checkpoint_id = updated_config["configurable"]["checkpoint_id"]
            cursor.execute(
                """UPDATE briefs SET content = %s, workflow_status = 'awaiting_review',
                       final_output = NULL, workflow_checkpoint_id = %s,
                       workflow_version = workflow_version + 1, updated_at = %s
                   WHERE id = %s AND workflow_version = %s RETURNING workflow_version""",
                (Jsonb(new_content), checkpoint_id, datetime.now(), brief.id, row["workflow_version"]),
            )
            if cursor.fetchone() is None:
                raise HTTPException(409, "营销原稿已更新，请重新创建改稿任务")

    # Recovered heads may already contain the persistent human-review interrupt.
    current = graph.get_state(config)
    if not pending_human_review(current):
        if tuple(current.next or ()) == ("human_review",):
            graph.invoke(None, config)
        elif not (already_published and not current.next and (current.values or {}).get("final_output")):
            raise HTTPException(409, "营销流程已继续执行，不能重复采纳旧改稿")
    refreshed = _load_execution_brief(guard, brief.user_id, team_id=brief.team_id)
    current = graph.get_state(config)
    if (
        refreshed.workflow_checkpoint_id == snapshot_checkpoint_id(current)
        and content_hash(refreshed.content) == content_hash(new_content)
    ):
        response = to_response(refreshed, current)
    else:
        response = persist_snapshot(refreshed, current)
    return {
        "checkpoint_id": response.checkpoint_id,
        "workflow_version": refreshed.workflow_version,
        "workflow_status": response.status,
        "content": dump_content(response.content),
    }


def iter_start_stream(user_id: UUID, brief_id: UUID, *, team_id: UUID) -> Iterator[bytes]:
    return preserve_execution_context(_iter_start_stream(user_id, brief_id, team_id=team_id))


def _iter_start_stream(user_id, brief_id, *, team_id):
    try:
        with workflow_execution(brief_id) as guard:
            brief = _load_execution_brief(guard, user_id, team_id=team_id)
            config = graph_config(brief)
            snapshot = graph.get_state(config)
            _validate_snapshot_scope(brief, snapshot)
            should_run, graph_input = _start_input(brief, snapshot)
            if should_run:
                first = (snapshot.next or ("retrieve_context",))[0]
                for event in iter_graph_events(
                    graph_input, config, first,
                    authorize=lambda: _load_execution_brief(guard, user_id, team_id=team_id),
                ):
                    yield encode_stream_event(event)
            response = persist_snapshot(brief, graph.get_state(config))
            yield encode_stream_event({"type": "done", "workflow": response})
    except Exception as exc:
        yield encode_stream_event(stream_error_event(exc))


def iter_review_stream(user_id: UUID, payload: MarketingWorkflowReviewInput, *, team_id: UUID) -> Iterator[bytes]:
    return preserve_execution_context(_iter_review_stream(user_id, payload, team_id=team_id))


def _iter_review_stream(user_id, payload, *, team_id):
    try:
        with workflow_execution(payload.brief_id) as guard:
            brief = _load_execution_brief(guard, user_id, team_id=team_id)
            config = graph_config(brief)
            snapshot = graph.get_state(config)
            _validate_snapshot_scope(brief, snapshot)
            validate_review(snapshot, payload)
            current_revisions = int((snapshot.values or {}).get("revision_count") or 0)
            if payload.action == "approved":
                first_node = "final_output"
            elif revisions_exhausted(current_revisions + 1):
                first_node = "max_revisions"
            else:
                first_node = "generate_strategy"
            command = Command(resume={
                "action": payload.action, "feedback": payload.feedback or "",
                "source_checkpoint_id": payload.expected_checkpoint_id,
            })
            for event in iter_graph_events(
                command, config, first_node,
                authorize=lambda: _load_execution_brief(guard, user_id, team_id=team_id),
            ):
                yield encode_stream_event(event)
            response = persist_snapshot(brief, graph.get_state(config))
            yield encode_stream_event({"type": "done", "workflow": response})
    except Exception as exc:
        yield encode_stream_event(stream_error_event(exc))

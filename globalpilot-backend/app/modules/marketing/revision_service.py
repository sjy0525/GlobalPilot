"""Authorized, durable revision tasks around the LangGraph execution engine.

All mutations use the physical connection holding the Brief execution lock.
No transaction or team lock remains open while a model is executing. Source,
evidence, task tokens and permissions are checked again at write boundaries.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from copy import deepcopy
from datetime import datetime
from difflib import SequenceMatcher
import hashlib
import json
import re
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from app.core.http_errors import AppHTTPException
from app.modules.briefs.authorization import load_owned_team_brief_cursor, require_owned_team_brief
from app.modules.marketing.models import RevisionTask, RevisionSample
from app.modules.marketing.schemas.revision import (
    BrandEvidence, DraftBlock, DraftCandidate, DraftSnapshot,
    RevisionRequirements, RevisionReviewContext, RevisionTaskInput,
    SemanticValidationResult,
)
from app.modules.marketing.workflow.execution import workflow_execution
from app.modules.marketing.workflow.revision.agent import invoke_semantic_reviewer
from app.modules.marketing.workflow.revision.graph import build_revision_graph
from app.modules.marketing.workflow.revision.validation import (
    semantic_input_hash, validate_draft, validate_semantics,
)


def _fail(status: int, message: str, code: str = "REVISION_CONFLICT"):
    raise AppHTTPException(status, message, code=code)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _sessions():
    from app.db.database import sessionLocal
    return sessionLocal


class RevisionPostgresSaver(PostgresSaver):
    """Task-ID checkpoints fenced by the authorized Brief's physical session."""

    def __init__(self, guard, task_id: UUID):
        super().__init__(guard.connection)
        self.guard = guard
        self.task_id = str(task_id)

    def _check(self, config):
        values = config.get("configurable", {})
        if str(values.get("thread_id")) != self.task_id:
            raise RuntimeError("Revision checkpointer does not own this task")
        if values.get("checkpoint_ns", ""):
            raise RuntimeError("Revision task checkpoints require their own root thread")

    def get_tuple(self, config):
        self._check(config)
        with self.guard.checkpoint_saver(self.serde) as saver:
            return saver.get_tuple(config)

    def list(self, config, *, filter=None, before=None, limit=None) -> Iterator:
        if config is None:
            raise RuntimeError("Revision checkpoint listing requires task scope")
        self._check(config)
        if before is not None:
            self._check(before)
        with self.guard.checkpoint_saver(self.serde) as saver:
            yield from saver.list(config, filter=filter, before=before, limit=limit)

    def put(self, config, checkpoint, metadata, new_versions):
        self._check(config)
        with self.guard.checkpoint_saver(self.serde) as saver:
            return saver.put(config, checkpoint, metadata, new_versions)

    def put_writes(self, config, writes, task_id, task_path=""):
        self._check(config)
        with self.guard.checkpoint_saver(self.serde) as saver:
            return saver.put_writes(config, writes, task_id, task_path)

    def get_delta_channel_history(self, *, config, channels: Sequence[str]):
        self._check(config)
        with self.guard.checkpoint_saver(self.serde) as saver:
            return saver.get_delta_channel_history(config=config, channels=channels)

    def delete_thread(self, thread_id):
        if str(thread_id) != self.task_id:
            raise RuntimeError("Revision checkpointer does not own this task")
        with self.guard.checkpoint_saver(self.serde) as saver:
            saver.delete_thread(thread_id)

    def copy_thread(self, *args, **kwargs):
        raise NotImplementedError("Revision checkpoint copying is not supported")

    def delete_for_runs(self, *args, **kwargs):
        raise NotImplementedError("Revision checkpoint deletion needs explicit task scope")

    def prune(self, *args, **kwargs):
        raise NotImplementedError("Revision checkpoint pruning needs explicit task scope")


def _brief(guard, actor_user_id, team_id, *, lock=False):
    with guard.transaction() as cursor:
        if lock:
            cursor.execute("SELECT id FROM teams WHERE id = %s FOR UPDATE", (team_id,))
        return load_owned_team_brief_cursor(
            cursor, team_id=team_id, actor_user_id=actor_user_id,
            brief_id=guard.brief_id,
        )


def _load_task(guard, task_id, actor_user_id, team_id, *, lock=False):
    with guard.transaction() as cursor:
        if lock:
            cursor.execute("SELECT id FROM teams WHERE id = %s FOR UPDATE", (team_id,))
        load_owned_team_brief_cursor(
            cursor, team_id=team_id, actor_user_id=actor_user_id,
            brief_id=guard.brief_id,
        )
        cursor.execute(
            """SELECT * FROM marketing_revision_tasks
               WHERE id = %s AND brief_id = %s AND team_id = %s AND user_id = %s""",
            (task_id, guard.brief_id, team_id, actor_user_id),
        )
        row = cursor.fetchone()
    if row is None:
        _fail(404, "改稿任务不存在", "REVISION_TASK_NOT_FOUND")
    return row


def _owned_task(db, user, task_id, team_id):
    row = db.query(RevisionTask).filter(
        RevisionTask.id == task_id, RevisionTask.team_id == team_id,
        RevisionTask.user_id == user.id,
    ).populate_existing().one_or_none()
    if row is None:
        _fail(404, "改稿任务不存在", "REVISION_TASK_NOT_FOUND")
    require_owned_team_brief(db, team_id=team_id, actor_user_id=user.id, brief_id=row.brief_id)
    return row


def _parent_source_is_current(brief):
    """Projection and actual parent head must identify the same manuscript."""
    from app.modules.marketing import service as parent
    if not brief.get("workflow_checkpoint_id"):
        _fail(409, "请先生成并保存营销文案", "REVISION_SOURCE_NOT_READY")
    item = SimpleNamespace(**brief)
    snapshot = parent.graph.get_state(parent.graph_config(item))
    parent._validate_snapshot_scope(item, snapshot)
    values = snapshot.values or {}
    if (
        parent.snapshot_checkpoint_id(snapshot) != brief["workflow_checkpoint_id"]
        or parent.dump_content(values.get("content")) != brief["content"]
    ):
        _fail(409, "营销流程与文稿版本未同步，请先恢复营销流程", "REVISION_SOURCE_NOT_READY")


def _body(brief, platform):
    content = brief.get("content")
    platforms = content.get("platforms") if isinstance(content, dict) else None
    value = platforms.get(platform) if isinstance(platforms, dict) else None
    body = value.get("body") if isinstance(value, dict) else None
    if not isinstance(body, str) or not body.strip():
        _fail(409, "该平台没有可修改的正文", "REVISION_SOURCE_NOT_READY")
    return body


def split_document_body(body: str, previous_blocks: list[dict] | None = None):
    """Keep every separator byte while assigning stable paragraph identities."""
    texts, layout = [], []
    pending = ""
    for index, piece in enumerate(re.split(r"((?:\r?\n[ \t]*){2,})", body)):
        if index % 2 or not piece.strip():
            pending += piece
        else:
            layout.append(pending)
            texts.append(piece)
            pending = ""
    layout.append(pending)
    if not texts:
        _fail(409, "文稿正文不能只包含空白字符", "REVISION_SOURCE_NOT_READY")
    identifiers = [str(uuid4()) for _ in texts]
    old = previous_blocks or []
    matcher = SequenceMatcher(a=[item["text"] for item in old], b=texts, autojunk=False)
    for kind, left_start, left_end, right_start, right_end in matcher.get_opcodes():
        if kind == "equal" or (kind == "replace" and left_end - left_start == right_end - right_start):
            for left, right in zip(range(left_start, left_end), range(right_start, right_end)):
                identifiers[right] = old[left]["block_id"]
    blocks = [{"block_id": identifier, "text": text} for identifier, text in zip(identifiers, texts)]
    return blocks, layout


def join_document_body(blocks: list[DraftBlock] | list[dict], layout: list[str]) -> str:
    if len(layout) != len(blocks) + 1:
        raise ValueError("文稿段落布局不匹配")
    return "".join(
        layout[index] + (block.text if isinstance(block, DraftBlock) else block["text"])
        for index, block in enumerate(blocks)
    ) + layout[-1]


def _source_snapshot(guard, brief, platform):
    _parent_source_is_current(brief)
    body = _body(brief, platform)
    body_hash = _digest(body)
    with guard.transaction() as cursor:
        cursor.execute("SELECT id FROM teams WHERE id = %s FOR UPDATE", (brief["team_id"],))
        load_owned_team_brief_cursor(cursor, team_id=brief["team_id"], actor_user_id=brief["user_id"], brief_id=brief["id"])
        cursor.execute(
            "SELECT * FROM marketing_revision_drafts WHERE brief_id = %s AND platform = %s FOR UPDATE",
            (brief["id"], platform),
        )
        row = cursor.fetchone()
        if row is None:
            blocks, layout = split_document_body(body)
            cursor.execute(
                """INSERT INTO marketing_revision_drafts
                   (id,team_id,user_id,brief_id,platform,version,body_hash,blocks,layout,created_at,updated_at)
                   VALUES (%s,%s,%s,%s,%s,1,%s,%s,%s,%s,%s) RETURNING *""",
                (uuid4(), brief["team_id"], brief["user_id"], brief["id"], platform,
                 body_hash, Jsonb(blocks), Jsonb(layout), datetime.now(), datetime.now()),
            )
            row = cursor.fetchone()
        elif row["body_hash"] != body_hash:
            blocks, layout = split_document_body(body, row["blocks"])
            cursor.execute(
                """UPDATE marketing_revision_drafts SET version = version + 1,
                   body_hash=%s,blocks=%s,layout=%s,updated_at=%s WHERE id=%s RETURNING *""",
                (body_hash, Jsonb(blocks), Jsonb(layout), datetime.now(), row["id"]),
            )
            row = cursor.fetchone()
    snapshot = DraftSnapshot(
        draft_id=row["id"], version=row["version"], source_brief_id=brief["id"],
        source_checkpoint_id=brief["workflow_checkpoint_id"], platform=platform,
        blocks=row["blocks"],
    )
    return snapshot, row["layout"]


def get_revision_document(db, user, brief_id, platform, *, team_id):
    actor = user.id
    require_owned_team_brief(db, team_id=team_id, actor_user_id=actor, brief_id=brief_id)
    db.rollback()
    with workflow_execution(brief_id) as guard:
        snapshot, _ = _source_snapshot(guard, _brief(guard, actor, team_id), platform)
        return snapshot


def _query_terms(query: str) -> set[str]:
    terms = {item.lower() for item in re.findall(r"[A-Za-z0-9][A-Za-z0-9._+-]*", query)}
    for part in re.findall(r"[\u3400-\u9fff]+", query):
        terms.update(part[index:index + 2] for index in range(len(part) - 1))
    return {item for item in terms if len(item) >= 2}


def _search_context(guard, brief, *, query, brand_rules, selected_ids):
    """Search only authorized current documents, returning bounded exact quotes."""
    with guard.transaction() as cursor:
        load_owned_team_brief_cursor(cursor, team_id=brief["team_id"], actor_user_id=brief["user_id"], brief_id=brief["id"])
        cursor.execute(
            """SELECT d.id,d.version,d.source,d.body FROM knowledge_documents d
               JOIN brands b ON b.id=d.brand_id AND b.team_id=d.team_id
               WHERE d.team_id=%s AND d.brand_id=%s ORDER BY d.id""",
            (brief["team_id"], brief["brand_id"]),
        )
        rows = cursor.fetchall()
    by_id = {str(row["id"]): row for row in rows}
    if selected_ids:
        if any(str(identifier) not in by_id for identifier in selected_ids):
            _fail(404, "所选品牌资料不存在或无权访问", "REVISION_EVIDENCE_NOT_FOUND")
        rows = [by_id[str(identifier)] for identifier in selected_ids]
    terms = _query_terms(query)
    ranked = []
    for row in rows:
        text = row["body"] or ""
        if not text.strip():
            continue
        lower = text.lower()
        matches = [lower.find(term) for term in terms if term in lower]
        if not matches and not selected_ids:
            continue
        start = max(0, min(matches, default=0) - 120)
        quote = text[start:start + 1200]
        ranked.append((len(matches), row, quote))
    ranked.sort(key=lambda item: (-item[0], str(item[1]["id"])))
    evidence = [
        BrandEvidence(
            evidence_id=f"{row['id']}:v{row['version']}:{hashlib.sha256(quote.encode()).hexdigest()[:12]}",
            document_id=row["id"], document_version=row["version"],
            source=row["source"], quote=quote,
        )
        for _, row, quote in ranked[:6]
    ]
    return RevisionReviewContext(
        rules_version="revision-brand-rules:" + _digest(brand_rules),
        brand_rules=brand_rules, evidence=evidence,
    )


def _row_dict(record):
    return {column.name: getattr(record, column.name) for column in record.__table__.columns}


def _to_response(row):
    from app.modules.marketing.schemas.revision import RevisionTaskOutput
    state = row["state"] or {}
    issues = list((state.get("semantic_validation") or {}).get("issues") or [])
    issues.extend(state.get("execution_issues") or [])
    interrupt_payload = state.get("_interrupt")
    return RevisionTaskOutput(
        task_id=row["id"], brief_id=row["brief_id"], platform=row["platform"],
        status=row["status"], version=row["version"], checkpoint_id=row["checkpoint_id"],
        draft=row["original_draft"], requirements=row["requirements"],
        original_requirements=row["original_requirements"],
        review_context=row["review_context"], candidate=state.get("candidate"),
        validation=state.get("semantic_validation"), max_revisions=row["max_revisions"],
        revision_attempts=state.get("revision_attempts", 0), stop_reason=state.get("stop_reason"),
        issues=issues, interrupt=interrupt_payload, history=row["history"],
        adopted_version=row["adopted_version"], created_at=row["created_at"], updated_at=row["updated_at"],
    )


def get_revision_task(db, user, task_id, *, team_id):
    return _to_response(_row_dict(_owned_task(db, user, task_id, team_id)))


def list_revision_tasks(db, user, brief_id, *, team_id):
    require_owned_team_brief(db, team_id=team_id, actor_user_id=user.id, brief_id=brief_id)
    rows = db.query(RevisionTask).filter(
        RevisionTask.brief_id == brief_id, RevisionTask.team_id == team_id,
        RevisionTask.user_id == user.id,
    ).order_by(RevisionTask.created_at.desc()).all()
    return [_to_response(_row_dict(row)) for row in rows]


def create_revision_task(db, user, payload, *, team_id):
    actor = user.id
    require_owned_team_brief(db, team_id=team_id, actor_user_id=actor, brief_id=payload.brief_id)
    request = payload.model_dump(mode="json")
    request_hash = _digest(request)
    db.rollback()
    with workflow_execution(payload.brief_id) as guard:
        brief = _brief(guard, actor, team_id)
        with guard.transaction() as cursor:
            cursor.execute(
                """SELECT * FROM marketing_revision_tasks
                   WHERE team_id=%s AND user_id=%s AND idempotency_key=%s""",
                (team_id, actor, payload.idempotency_key),
            )
            duplicate = cursor.fetchone()
        if duplicate:
            if duplicate["request_hash"] != request_hash:
                _fail(409, "幂等键已用于不同的改稿请求", "REVISION_IDEMPOTENCY_CONFLICT")
            return _to_response(duplicate)
        snapshot, layout = _source_snapshot(guard, brief, payload.platform)
        if snapshot.draft_id != payload.expected_draft_id or snapshot.version != payload.expected_draft_version:
            _fail(409, "原稿已更新，请重新读取段落", "REVISION_SOURCE_CONFLICT")
        _validate_requested_task(snapshot, payload.requirements)
        query = payload.requirements.goal + "\n" + "\n".join(
            block.text for block in snapshot.blocks
            if block.block_id in set(payload.requirements.allowed_block_ids)
        )
        context = _search_context(guard, brief, query=query, brand_rules=payload.brand_rules, selected_ids=payload.evidence_document_ids)
        now, identifier = datetime.now(), uuid4()
        history = [{"kind": "created", "at": now.isoformat(), "request": request}]
        with guard.transaction() as cursor:
            cursor.execute("SELECT id FROM teams WHERE id=%s FOR UPDATE", (team_id,))
            load_owned_team_brief_cursor(cursor, team_id=team_id, actor_user_id=actor, brief_id=brief["id"])
            # Different Brief execution locks can race on the same actor's key.
            # The shared team row serializes this final duplicate check/insert.
            cursor.execute(
                """SELECT * FROM marketing_revision_tasks
                   WHERE team_id=%s AND user_id=%s AND idempotency_key=%s""",
                (team_id, actor, payload.idempotency_key),
            )
            duplicate = cursor.fetchone()
            if duplicate is not None:
                if duplicate["request_hash"] != request_hash:
                    _fail(409, "幂等键已用于不同的改稿请求", "REVISION_IDEMPOTENCY_CONFLICT")
                return _to_response(duplicate)
            cursor.execute(
                """INSERT INTO marketing_revision_tasks
                (id,team_id,user_id,brief_id,draft_id,platform,status,version,checkpoint_id,max_revisions,
                 idempotency_key,request_hash,original_draft,original_requirements,requirements,
                 review_context,selected_document_ids,source_content,source_content_hash,
                 source_workflow_version,source_layout,state,history,created_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,'created',1,NULL,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING *""",
                (identifier, team_id, actor, brief["id"], snapshot.draft_id, payload.platform,
                 payload.max_revisions, payload.idempotency_key, request_hash,
                 Jsonb(snapshot.model_dump(mode="json")), Jsonb(payload.requirements.model_dump(mode="json")),
                 Jsonb(payload.requirements.model_dump(mode="json")), Jsonb(context.model_dump(mode="json")),
                 Jsonb([str(value) for value in payload.evidence_document_ids]), Jsonb(brief["content"]),
                 _digest(brief["content"]), brief["workflow_version"], Jsonb(layout), Jsonb({}), Jsonb(history), now, now),
            )
            row = cursor.fetchone()
        return _to_response(row)


def _task_input(row):
    return RevisionTaskInput(draft=row["original_draft"], requirements=row["requirements"])


def _validate_requested_task(draft, requirements):
    try:
        return RevisionTaskInput(draft=draft, requirements=requirements)
    except ValidationError as exc:
        _fail(
            422,
            [{"loc": error["loc"], "msg": error["msg"]} for error in exc.errors()],
            "REVISION_REQUIREMENTS_INVALID",
        )


def _new_content(row, candidate):
    content = deepcopy(row["source_content"])
    content["platforms"][row["platform"]]["body"] = join_document_body(candidate.blocks, row["source_layout"])
    return content


def _assert_source_current(guard, row, *, own_content=None):
    brief = _brief(guard, row["user_id"], row["team_id"])
    if own_content is not None and _digest(brief["content"]) == _digest(own_content):
        from app.modules.marketing import service as parent
        head = parent.graph.get_state(parent.graph_config(SimpleNamespace(**brief)))
        if str((head.values or {}).get("targeted_revision_task_id")) == str(row["id"]):
            return brief
    _parent_source_is_current(brief)
    if (
        brief["workflow_checkpoint_id"] != row["original_draft"]["source_checkpoint_id"]
        or _digest(brief["content"]) != row["source_content_hash"]
    ):
        _fail(409, "原稿或营销流程已更新，请基于最新文稿重新创建任务", "REVISION_SOURCE_CONFLICT")
    return brief


def _assert_evidence_current(guard, row, context=None):
    context = context or RevisionReviewContext.model_validate(row["review_context"])
    brief = _brief(guard, row["user_id"], row["team_id"])
    with guard.transaction() as cursor:
        for evidence in context.evidence:
            cursor.execute(
                """SELECT version,body FROM knowledge_documents
                   WHERE id=%s AND team_id=%s AND brand_id=%s""",
                (evidence.document_id, row["team_id"], brief["brand_id"]),
            )
            source = cursor.fetchone()
            if (
                source is None or source["version"] != evidence.document_version
                or evidence.quote not in (source["body"] or "")
            ):
                _fail(409, "品牌证据已更新，请重新补充资料并检查", "REVISION_EVIDENCE_CONFLICT")


def _checkpoint_id(snapshot):
    return (snapshot.config or {}).get("configurable", {}).get("checkpoint_id")


def _interrupt_value(snapshot):
    for task in snapshot.tasks or ():
        if task.result is None:
            for item in task.interrupts:
                value = item.value
                if isinstance(value, dict):
                    return value
    return None


def _graph_for(guard, row, *, reviewer=None, reviser=None):
    def document_reader():
        latest = _load_task(guard, row["id"], row["user_id"], row["team_id"])
        _assert_source_current(guard, latest)
        return _task_input(latest)

    def evidence_searcher(query):
        latest = _load_task(guard, row["id"], row["user_id"], row["team_id"])
        brief = _assert_source_current(guard, latest)
        existing = RevisionReviewContext.model_validate(latest["review_context"])
        context = _search_context(
            guard, brief, query=query, brand_rules=existing.brand_rules,
            selected_ids=latest["selected_document_ids"],
        )
        with guard.transaction() as cursor:
            cursor.execute("SELECT id FROM teams WHERE id=%s FOR UPDATE", (row["team_id"],))
            load_owned_team_brief_cursor(cursor, team_id=row["team_id"], actor_user_id=row["user_id"], brief_id=row["brief_id"])
            cursor.execute(
                "UPDATE marketing_revision_tasks SET review_context=%s WHERE id=%s",
                (Jsonb(context.model_dump(mode="json")), row["id"]),
            )
        return context

    options = {
        "checkpointer": RevisionPostgresSaver(guard, row["id"]),
        "max_revisions": row["max_revisions"], "interactive": True,
        "document_reader": document_reader, "evidence_searcher": evidence_searcher,
    }
    if reviewer is not None:
        options["reviewer"] = reviewer
    if reviser is not None:
        options["reviser"] = reviser
    return build_revision_graph(**options)


def _graph_config(row):
    return {"configurable": {"thread_id": str(row["id"]), "checkpoint_ns": ""}, "recursion_limit": 40}


def _project(guard, row, snapshot):
    values = deepcopy(snapshot.values or {})
    interrupted = _interrupt_value(snapshot)
    if interrupted is not None:
        values["_interrupt"] = interrupted
    status = values.get("status") or "created"
    if status == "accepted" and row["adopted_version"] is None:
        # A saved human decision is not yet a published manuscript or sample.
        status = "adopting"
    requirements = (values.get("task") or {}).get("requirements") or row["requirements"]
    context = values.get("review_context") or row["review_context"]
    history = list(row["history"])
    history.append({"kind": "checkpoint", "checkpoint_id": _checkpoint_id(snapshot), "status": status, "at": datetime.now().isoformat()})
    with guard.transaction() as cursor:
        cursor.execute("SELECT id FROM teams WHERE id=%s FOR UPDATE", (row["team_id"],))
        load_owned_team_brief_cursor(cursor, team_id=row["team_id"], actor_user_id=row["user_id"], brief_id=row["brief_id"])
        cursor.execute(
            """UPDATE marketing_revision_tasks SET status=%s,state=%s,requirements=%s,
               review_context=%s,checkpoint_id=%s,version=version+1,history=%s,updated_at=%s
               WHERE id=%s RETURNING *""",
            (status, Jsonb(values), Jsonb(requirements), Jsonb(context), _checkpoint_id(snapshot),
             Jsonb(history), datetime.now(), row["id"]),
        )
        return cursor.fetchone()


def _begin_operation(guard, row, kind, payload, snapshot):
    data = payload.model_dump(mode="json") if payload is not None else {
        "expected_task_version": row["version"], "expected_checkpoint_id": row["checkpoint_id"],
        "idempotency_key": f"run:{row['version']}",
    }
    key, fingerprint = data["idempotency_key"], _digest({"kind": kind, "payload": data})
    with guard.transaction() as cursor:
        cursor.execute("SELECT id FROM teams WHERE id=%s FOR UPDATE", (row["team_id"],))
        load_owned_team_brief_cursor(cursor, team_id=row["team_id"], actor_user_id=row["user_id"], brief_id=row["brief_id"])
        cursor.execute("SELECT * FROM marketing_revision_operations WHERE task_id=%s AND idempotency_key=%s", (row["id"], key))
        existing = cursor.fetchone()
        if existing:
            if existing["request_hash"] != fingerprint:
                _fail(409, "幂等键已用于不同的操作", "REVISION_IDEMPOTENCY_CONFLICT")
            if existing["status"] == "failed":
                # A known pre-execution failure may be retried only while its
                # original state is still current. Unlike an unfinished crash
                # recovery, it cannot claim another operation's later progress.
                if (
                    data["expected_task_version"] != row["version"]
                    or data.get("expected_checkpoint_id") != _checkpoint_id(snapshot)
                ):
                    _fail(409, "任务已更新，请刷新后重新提交", "REVISION_TASK_CONFLICT")
                cursor.execute(
                    "SELECT id FROM marketing_revision_operations WHERE task_id=%s AND status='running' AND id<>%s",
                    (row["id"], existing["id"]),
                )
                if cursor.fetchone() is not None:
                    _fail(409, "已有未完成操作，请使用原幂等键重试", "REVISION_OPERATION_PENDING")
                cursor.execute("UPDATE marketing_revision_operations SET status='running',updated_at=%s WHERE id=%s", (datetime.now(), existing["id"]))
                existing["status"] = "running"
            if existing["status"] != "completed":
                cursor.execute("UPDATE marketing_revision_tasks SET status='running',updated_at=%s WHERE id=%s", (datetime.now(), row["id"]))
            return existing
        cursor.execute("SELECT id FROM marketing_revision_operations WHERE task_id=%s AND status='running'", (row["id"],))
        if cursor.fetchone() is not None:
            _fail(409, "已有未完成操作，请使用原幂等键重试", "REVISION_OPERATION_PENDING")
        if data["expected_task_version"] != row["version"]:
            _fail(409, "任务已更新，请刷新后重试", "REVISION_TASK_CONFLICT")
        current_checkpoint = _checkpoint_id(snapshot)
        if data.get("expected_checkpoint_id") != current_checkpoint:
            _fail(409, "检查点已更新，请刷新后重试", "REVISION_CHECKPOINT_CONFLICT")
        now = datetime.now()
        stored_payload = dict(data)
        if kind == "review":
            stored_payload["_model_candidate"] = (row["state"] or {}).get("candidate")
        elif kind == "clarify":
            stored_payload["_requirements_before"] = row["requirements"]
            stored_payload["_selected_document_ids_before"] = row["selected_document_ids"]
        cursor.execute(
            """INSERT INTO marketing_revision_operations
               (id,task_id,idempotency_key,kind,request_hash,payload,status,response,created_at,updated_at)
               VALUES (%s,%s,%s,%s,%s,%s,'running',NULL,%s,%s) RETURNING *""",
            (uuid4(), row["id"], key, kind, fingerprint, Jsonb(stored_payload), now, now),
        )
        operation = cursor.fetchone()
        cursor.execute("UPDATE marketing_revision_tasks SET status='running',updated_at=%s WHERE id=%s", (now, row["id"]))
        return operation


def _complete_operation(guard, row, operation):
    response = _to_response(row)
    with guard.transaction() as cursor:
        cursor.execute("SELECT id FROM teams WHERE id=%s FOR UPDATE", (row["team_id"],))
        load_owned_team_brief_cursor(cursor, team_id=row["team_id"], actor_user_id=row["user_id"], brief_id=row["brief_id"])
        cursor.execute(
            "UPDATE marketing_revision_operations SET status='completed',response=%s,updated_at=%s WHERE id=%s",
            (Jsonb(response.model_dump(mode="json")), datetime.now(), operation["id"]),
        )
    return response


def _record_failure(guard, row, exc, *, operation, snapshot):
    # LangGraph can durably save a human answer in pending task writes before
    # its next checkpoint commits. That answer already belongs to this exact
    # operation even when the visible checkpoint ID has not advanced yet.
    progressed = (
        _checkpoint_id(snapshot) != operation["payload"].get("expected_checkpoint_id")
        or any(task.result is not None for task in snapshot.tasks or ())
    )
    status = "error" if progressed else ((snapshot.values or {}).get("status") or "created")
    with guard.transaction() as cursor:
        cursor.execute("SELECT id FROM teams WHERE id=%s FOR UPDATE", (row["team_id"],))
        load_owned_team_brief_cursor(cursor, team_id=row["team_id"], actor_user_id=row["user_id"], brief_id=row["brief_id"])
        cursor.execute("SELECT history FROM marketing_revision_tasks WHERE id=%s", (row["id"],))
        current = cursor.fetchone()
        failure = {
            "kind": "execution_failed", "error_type": type(exc).__name__,
            "at": datetime.now().isoformat(),
        }
        if isinstance(exc, AppHTTPException):
            failure.update(code=exc.code, detail=exc.detail)
        history = current["history"] + [failure]
        cursor.execute("UPDATE marketing_revision_tasks SET status=%s,history=%s,updated_at=%s WHERE id=%s", (status, Jsonb(history), datetime.now(), row["id"]))
        if not progressed:
            cursor.execute("UPDATE marketing_revision_operations SET status='failed',updated_at=%s WHERE id=%s", (datetime.now(), operation["id"]))


def _prepare_clarification(guard, row, payload, *, operation):
    brief = _assert_source_current(guard, row)
    # These baseline fields belong to the durable operation, not a projection
    # that this same operation may already have updated before a crash.
    requirements = dict(operation["payload"]["_requirements_before"])
    for field in ("goal", "must_keep", "length_limits"):
        value = getattr(payload, field)
        if value is not None:
            requirements[field] = [item.model_dump(mode="json") for item in value] if field == "length_limits" else value
    if payload.goal is None:
        requirements["goal"] += "\n补充修改要求：" + payload.feedback
    task = _validate_requested_task(row["original_draft"], requirements)
    old_context = RevisionReviewContext.model_validate(row["review_context"])
    selected = (
        [str(value) for value in payload.evidence_document_ids]
        or operation["payload"]["_selected_document_ids_before"]
    )
    query = task.requirements.goal + "\n" + payload.feedback + "\n" + "\n".join(block.text for block in task.draft.blocks if block.block_id in set(task.requirements.allowed_block_ids))
    context = _search_context(guard, brief, query=query, brand_rules=old_context.brand_rules, selected_ids=selected)
    with guard.transaction() as cursor:
        cursor.execute("SELECT id FROM teams WHERE id=%s FOR UPDATE", (row["team_id"],))
        load_owned_team_brief_cursor(cursor, team_id=row["team_id"], actor_user_id=row["user_id"], brief_id=row["brief_id"])
        cursor.execute(
            "UPDATE marketing_revision_tasks SET requirements=%s,review_context=%s,selected_document_ids=%s WHERE id=%s",
            (Jsonb(task.requirements.model_dump(mode="json")), Jsonb(context.model_dump(mode="json")), Jsonb(selected), row["id"]),
        )
    return {"task": task.model_dump(mode="json"), "review_context": context.model_dump(mode="json"), "feedback": payload.feedback}


def _prepare_review(guard, row, payload, values, *, reviewer=None):
    if payload.action == "rejected":
        return {"action": "rejected", "feedback": payload.feedback}
    _assert_source_current(guard, row)
    task = _task_input(row)
    context = RevisionReviewContext.model_validate(row["review_context"])
    _assert_evidence_current(guard, row, context)
    candidate = DraftCandidate.model_validate(values.get("candidate"))
    report = SemanticValidationResult.model_validate(values.get("semantic_validation"))
    if payload.blocks is not None and payload.blocks != candidate.blocks:
        try:
            candidate = DraftCandidate(candidate_id=uuid4(), draft_id=candidate.draft_id, base_version=candidate.base_version, blocks=payload.blocks)
        except ValidationError as exc:
            _fail(422, [{"loc": error["loc"], "msg": error["msg"]} for error in exc.errors()], "REVISION_HUMAN_EDIT_INVALID")
        program = validate_draft(task, candidate)
        if not program.passed:
            _fail(409, program.issues, "REVISION_HUMAN_EDIT_INVALID")
        report = validate_semantics(task, candidate, context, reviewer=reviewer or invoke_semantic_reviewer)
    if (
        report.status != "passed" or report.candidate_id != candidate.candidate_id
        or report.input_hash != semantic_input_hash(task, candidate, context)
        or not validate_draft(task, candidate).passed
    ):
        _fail(409, "人工采纳的候选未通过最新检查", "REVISION_REVIEW_NOT_PASSED")
    return {
        "action": "approved", "feedback": payload.feedback or "",
        "candidate": candidate.model_dump(mode="json"),
        "semantic_validation": report.model_dump(mode="json"),
    }


def _adopt(guard, row, operation):
    if row["adopted_version"] is not None:
        return row
    from app.modules.marketing.service import apply_targeted_revision_to_parent

    state = row["state"]
    candidate = DraftCandidate.model_validate(state["candidate"])
    context = RevisionReviewContext.model_validate(row["review_context"])
    report = SemanticValidationResult.model_validate(state["semantic_validation"])
    task = _task_input(row)
    if (
        report.status != "passed" or report.input_hash != semantic_input_hash(task, candidate, context)
        or report.candidate_id != candidate.candidate_id or not validate_draft(task, candidate).passed
    ):
        _fail(409, "候选或检查结果已变化，不能采纳", "REVISION_REVIEW_NOT_PASSED")

    content = _new_content(row, candidate)

    def before_publish():
        _assert_source_current(guard, row, own_content=content)
        _assert_evidence_current(guard, row, context)

    brief = _brief(guard, row["user_id"], row["team_id"])
    publication = apply_targeted_revision_to_parent(
        guard, SimpleNamespace(**brief), content,
        expected_checkpoint_id=row["original_draft"]["source_checkpoint_id"],
        revision_task_id=row["id"], before_publish=before_publish,
        revision_review_context=context,
    )
    now = datetime.now()
    model_candidate = operation["payload"].get("_model_candidate")
    edited = model_candidate is not None and model_candidate.get("blocks") != candidate.model_dump(mode="json")["blocks"]
    human_review = {
        "actor_user_id": str(row["user_id"]), "action": "approved",
        "feedback": operation["payload"].get("feedback") or "",
        "edited": edited, "at": now.isoformat(),
    }
    history = list(row["history"]) + [{"kind": "accepted", "human_review": human_review, "at": now.isoformat()}]
    with guard.transaction() as cursor:
        cursor.execute("SELECT id FROM teams WHERE id=%s FOR UPDATE", (row["team_id"],))
        load_owned_team_brief_cursor(cursor, team_id=row["team_id"], actor_user_id=row["user_id"], brief_id=row["brief_id"])
        cursor.execute("SELECT * FROM marketing_revision_drafts WHERE id=%s FOR UPDATE", (row["draft_id"],))
        draft = cursor.fetchone()
        expected_version = row["original_draft"]["version"]
        body_hash = _digest(content["platforms"][row["platform"]]["body"])
        if draft["version"] == expected_version:
            adopted_version = expected_version + 1
        elif draft["version"] == expected_version + 1 and draft["body_hash"] == body_hash:
            # A source GET may have synchronized an already-published parent
            # checkpoint after a crash but before this ledger transaction.
            adopted_version = draft["version"]
        else:
            _fail(409, "文稿版本已变化，不能覆盖当前结果", "REVISION_SOURCE_CONFLICT")
        cursor.execute(
            """UPDATE marketing_revision_drafts SET version=%s,blocks=%s,layout=%s,
               body_hash=%s,updated_at=%s WHERE id=%s""",
            (adopted_version, Jsonb([block.model_dump(mode="json") for block in candidate.blocks]),
             Jsonb(row["source_layout"]), body_hash, now, row["draft_id"]),
        )
        sample = {
            "schema_version": "targeted-revision-sample-v1", "task_id": str(row["id"]),
            "brief_id": str(row["brief_id"]), "platform": row["platform"],
            "original_draft": row["original_draft"], "original_requirements": row["original_requirements"],
            "requirements": row["requirements"], "review_context": row["review_context"],
            "model_candidate": model_candidate, "accepted_candidate": candidate.model_dump(mode="json"),
            "validation": report.model_dump(mode="json"), "human_review": human_review,
            "attempt_history": state.get("attempt_history") or [],
            "clarification_history": state.get("clarification_history") or [], "history": history,
            "source_checkpoint_id": row["original_draft"]["source_checkpoint_id"],
            "adopted_checkpoint_id": publication["checkpoint_id"], "adopted_version": adopted_version,
        }
        cursor.execute(
            """INSERT INTO marketing_revision_samples (id,task_id,team_id,user_id,brief_id,data,created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (task_id) DO NOTHING""",
            (uuid4(), row["id"], row["team_id"], row["user_id"], row["brief_id"], Jsonb(sample), now),
        )
        cursor.execute(
            """UPDATE marketing_revision_tasks SET adopted_version=%s,status='accepted',
               history=%s,version=version+1,updated_at=%s WHERE id=%s RETURNING *""",
            (adopted_version, Jsonb(history), now, row["id"]),
        )
        return cursor.fetchone()


def _execute_operation(user_id, task_id, payload, *, team_id, kind, reviewer=None, reviser=None):
    with _sessions()() as db:
        owned = _owned_task(db, SimpleNamespace(id=user_id), task_id, team_id)
        brief_id = owned.brief_id
    with workflow_execution(brief_id) as guard:
        row = _load_task(guard, task_id, user_id, team_id)
        graph = _graph_for(guard, row, reviewer=reviewer, reviser=reviser)
        config = _graph_config(row)
        snapshot = graph.get_state(config)
        operation = _begin_operation(guard, row, kind, payload, snapshot)
        if operation["status"] == "completed":
            from app.modules.marketing.schemas.revision import RevisionTaskOutput
            return RevisionTaskOutput.model_validate(operation["response"])
        try:
            values = snapshot.values or {}
            current_checkpoint = _checkpoint_id(snapshot)
            expected_checkpoint = operation["payload"].get("expected_checkpoint_id")
            pending = _interrupt_value(snapshot)
            saved_result = any(task.result is not None for task in snapshot.tasks or ())
            if kind == "run":
                if current_checkpoint is None:
                    _assert_source_current(guard, row)
                    graph.invoke({}, config)
                elif (snapshot.next or saved_result) and pending is None:
                    _assert_source_current(guard, row)
                    graph.invoke(None, config)
            elif current_checkpoint != expected_checkpoint:
                # This exact operation already advanced the durable graph. Never
                # apply its human answer a second time to a newer interrupt.
                if (snapshot.next or saved_result) and pending is None:
                    graph.invoke(None, config)
            elif pending is None and (snapshot.next or saved_result):
                # Pending task writes contain an answer whose next checkpoint
                # was interrupted. LangGraph must recover that saved answer.
                graph.invoke(None, config)
            else:
                wanted = "clarification" if kind == "clarify" else "review"
                if pending is None or pending.get("kind") != wanted:
                    _fail(409, "当前任务不在对应等待阶段", "REVISION_PHASE_CONFLICT")
                if kind == "clarify":
                    resume = _prepare_clarification(guard, row, payload, operation=operation)
                else:
                    resume = _prepare_review(guard, row, payload, values, reviewer=reviewer)
                graph.invoke(Command(resume=resume), config)
            snapshot = graph.get_state(config)
            row = _load_task(guard, task_id, user_id, team_id)
            row = _project(guard, row, snapshot)
            if (row["state"] or {}).get("status") == "accepted" and row["adopted_version"] is None:
                row = _adopt(guard, row, operation)
            return _complete_operation(guard, row, operation)
        except Exception as exc:
            try:
                _record_failure(guard, row, exc, operation=operation, snapshot=graph.get_state(config))
            except Exception:
                # A lost guard must not be replaced with a fresh connection in
                # order to write an error after execution ownership was lost.
                pass
            raise


def run_revision_task(user_id, task_id, payload=None, *, team_id, reviewer=None, reviser=None):
    return _execute_operation(user_id, task_id, payload, team_id=team_id, kind="run", reviewer=reviewer, reviser=reviser)


def clarify_revision_task(user_id, task_id, payload, *, team_id, reviewer=None, reviser=None):
    return _execute_operation(user_id, task_id, payload, team_id=team_id, kind="clarify", reviewer=reviewer, reviser=reviser)


def review_revision_task(user_id, task_id, payload, *, team_id, reviewer=None, reviser=None):
    return _execute_operation(user_id, task_id, payload, team_id=team_id, kind="review", reviewer=reviewer, reviser=reviser)


def export_revision_sample(db, user, task_id, *, team_id):
    row = _owned_task(db, user, task_id, team_id)
    sample = db.query(RevisionSample).filter(RevisionSample.task_id == row.id).one_or_none()
    if sample is None or row.status != "accepted":
        _fail(409, "该任务尚无人工采纳的业务样本", "REVISION_SAMPLE_NOT_READY")
    return deepcopy(sample.data)

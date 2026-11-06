from datetime import datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import any_, bindparam, func, select, text, update
from sqlalchemy.dialects.postgresql import ARRAY, UUID as PG_UUID, insert
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.modules.evaluation.engine.cases import CASES, TEST_SET_NAME, TEST_SET_VERSION, validate_catalog
from app.modules.evaluation.engine.report import render_comparison
from app.db.database import sessionLocal
from app.modules.evaluation.engine.lease import LeaseLost, claim_locked_slot, lease_is_live, lock_slot
from app.modules.evaluation.engine.runner import _fail_run, launch, recover_expired_run, recover_expired_runs, refresh_run_artifacts
from app.modules.evaluation.engine.scoring import DIMENSIONS, failure_reasons, is_qualified
from app.modules.evaluation.engine.versions import provenance_snapshot, workflow_version
from app.modules.evaluation.models import EvalCase, EvalRun, EvalTask, EvalTestSet
from app.modules.teams.authorization import (
    lock_team_and_require_member,
    require_active_team_member,
)
from app.modules.evaluation.schemas import (
    CompareRow,
    CompareSide,
    EvalCaseOut,
    EvalCompareOut,
    EvalReviewInput,
    EvalRunCreate,
    EvalRunOut,
    EvalTaskOut,
    EvalTestSetOut,
)


def ensure_test_set(db: Session, *, commit: bool = True) -> EvalTestSet:
    errors = validate_catalog()
    if errors:
        raise RuntimeError("评测集无效：" + "；".join(errors))
    existing = db.query(EvalTestSet).filter(EvalTestSet.version == TEST_SET_VERSION).one_or_none()
    if existing:
        return existing
    # The unique version constraint also arbitrates concurrent cold starts.
    created_id = db.scalar(
        insert(EvalTestSet).values(
            version=TEST_SET_VERSION,
            name=TEST_SET_NAME,
            description="30 条营销需求。每条保存用户需求、品牌资料、目标平台、必须包含的卖点和不能出现的说法。",
        ).on_conflict_do_nothing(index_elements=[EvalTestSet.version]).returning(EvalTestSet.id)
    )
    if created_id is None:
        return db.query(EvalTestSet).filter(EvalTestSet.version == TEST_SET_VERSION).one()
    test_set = db.get(EvalTestSet, created_id)
    for ordinal, row in enumerate(CASES, start=1):
        db.add(EvalCase(
            test_set_id=test_set.id,
            code=row["code"],
            ordinal=ordinal,
            title=row["title"],
            user_request=row["user_request"],
            brand_profile=row["brand_profile"],
            brand_name=row["brand_name"],
            platforms=row["platforms"],
            must_include=row["must_include"],
            forbidden_claims=row["forbidden_claims"],
            region=row["region"],
            persona=row["persona"],
            purchase_channels=row.get("purchase_channels"),
        ))
    db.flush()
    if commit:
        db.commit()
        db.refresh(test_set)
    return test_set


def recover_interrupted_runs(db: Session) -> None:
    # Startup cannot infer that another worker's live execution is interrupted.
    recover_expired_run(db, lock_slot(db))
    db.commit()


def get_test_set(db: Session, *, team_id: UUID, actor_user_id: UUID) -> EvalTestSetOut:
    require_active_team_member(db, team_id=team_id, user_id=actor_user_id)
    test_set = ensure_test_set(db)
    cases = (
        db.query(EvalCase)
        .filter(EvalCase.test_set_id == test_set.id)
        .order_by(EvalCase.ordinal)
        .all()
    )
    return EvalTestSetOut(
        id=test_set.id,
        version=test_set.version,
        name=test_set.name,
        description=test_set.description,
        cases=[EvalCaseOut.model_validate(case, from_attributes=True) for case in cases],
    )


def get_snapshot(db: Session, *, team_id: UUID, actor_user_id: UUID) -> dict:
    require_active_team_member(db, team_id=team_id, user_id=actor_user_id)
    test_set = ensure_test_set(db)
    return provenance_snapshot(db, test_set.version, team_id=team_id)


def _task_counts(db: Session, run_ids: list[UUID]) -> dict[UUID, tuple[int, int]]:
    if not run_ids:
        return {}
    # One array bind also handles long histories without PostgreSQL's limit on
    # the number of bind parameters. Empty runs receive (0, 0) in the serializer.
    rows = db.execute(
        select(
            EvalTask.run_id,
            func.count().label("total"),
            func.count().filter(EvalTask.execution_status.notin_(("pending", "running"))).label("done"),
        )
        .where(EvalTask.run_id == any_(bindparam("run_ids", run_ids, type_=ARRAY(PG_UUID(as_uuid=True)))))
        .group_by(EvalTask.run_id)
    )
    return {run_id: (total, done) for run_id, total, done in rows}


def _run_out(db: Session, run: EvalRun, counts: tuple[int, int] | None = None) -> EvalRunOut:
    total, done = counts if counts is not None else _task_counts(db, [run.id]).get(run.id, (0, 0))
    return EvalRunOut(
        id=run.id,
        team_id=run.team_id,
        label=run.label,
        status=run.status,
        is_baseline=run.is_baseline,
        provenance=run.provenance,
        summary=run.summary,
        report_markdown=run.report_markdown,
        error=run.error,
        started_at=run.started_at,
        finished_at=run.finished_at,
        created_at=run.created_at,
        task_total=total,
        task_done=done,
    )


def _recover_before_read(db: Session, user, *, team_id: UUID) -> UUID:
    user_id = user.id
    require_active_team_member(db, team_id=team_id, user_id=user_id)
    # End the read-only authentication transaction before the recovery Session
    # checks out a connection. The subsequent read also gets fresh ORM state.
    db.rollback()
    recover_expired_runs()
    # A response combines run summaries, task rows and counts. Pin one snapshot
    # before any of those reads so a concurrent review cannot mix revisions.
    # Set isolation on a newly checked-out connection, before its first query.
    db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
    db.execute(text("SET TRANSACTION READ ONLY"))
    require_active_team_member(db, team_id=team_id, user_id=user_id)
    return user_id


def list_runs(db: Session, user, *, team_id: UUID) -> list[EvalRunOut]:
    user_id = _recover_before_read(db, user, team_id=team_id)
    runs = (
        db.query(EvalRun)
        .filter(EvalRun.user_id == user_id, EvalRun.team_id == team_id)
        .order_by(EvalRun.created_at.desc())
        .all()
    )
    counts = _task_counts(db, [run.id for run in runs])
    return [_run_out(db, run, counts.get(run.id, (0, 0))) for run in runs]


def get_run(db: Session, user, run_id: UUID, *, team_id: UUID) -> EvalRunOut:
    user_id = _recover_before_read(db, user, team_id=team_id)
    return _run_out(db, _owned_run(db, user_id, run_id, team_id=team_id))


def list_tasks(db: Session, user, run_id: UUID, *, team_id: UUID) -> list[EvalTaskOut]:
    user_id = _recover_before_read(db, user, team_id=team_id)
    _owned_run(db, user_id, run_id, team_id=team_id)
    rows = (
        db.query(EvalTask, EvalCase)
        .join(EvalCase, EvalCase.id == EvalTask.case_id)
        .filter(EvalTask.run_id == run_id)
        .order_by(EvalCase.ordinal)
        .all()
    )
    return [_task_out(task, case) for task, case in rows]


def start_run(user_id: UUID, body: EvalRunCreate, *, team_id: UUID) -> EvalRunOut:
    # Authentication has already used the request Session. A dedicated Session
    # gives this write a single explicit transaction, without nested autobegin.
    with sessionLocal.begin() as db:
        lock_team_and_require_member(db, team_id=team_id, actor_user_id=user_id)
        current = workflow_version()
        if body.workflow_version != current:
            raise HTTPException(
                status_code=409,
                detail=f"当前代码的工作流版本是 {current}，不能按 {body.workflow_version} 执行",
            )
        lease = lock_slot(db)
        if lease_is_live(db, lease):
            raise HTTPException(status_code=409, detail="已有评测在运行，请等它结束后再开始")
        recover_expired_run(db, lease)
        test_set = ensure_test_set(db, commit=False)
        run = EvalRun(
            team_id=team_id,
            user_id=user_id,
            test_set_id=test_set.id,
            label=body.label or datetime.now().strftime("运行 %m-%d %H:%M"),
            status="queued",
            provenance=provenance_snapshot(db, test_set.version, team_id=team_id),
        )
        db.add(run)
        db.flush()
        cases = db.scalars(
            select(EvalCase).where(EvalCase.test_set_id == test_set.id).order_by(EvalCase.ordinal)
        ).all()
        for case in cases:
            db.add(EvalTask(run_id=run.id, case_id=case.id, execution_status="pending"))
        token = claim_locked_slot(db, lease, run.id)
        result = _run_out(db, run)
    try:
        launch(token)
    except Exception:
        try:
            _fail_run(token, "评测没有启动")
        except LeaseLost:
            pass
        raise
    return result


def _lock_owned_run(db: Session, user_id: UUID, run_id: UUID, *, team_id: UUID) -> EvalRun:
    run = db.scalar(
        select(EvalRun)
        .where(EvalRun.id == run_id, EvalRun.user_id == user_id, EvalRun.team_id == team_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if run is None:
        raise HTTPException(status_code=404, detail="评测运行不存在")
    return run


def set_baseline(user_id: UUID, run_id: UUID, *, team_id: UUID) -> EvalRunOut:
    with sessionLocal.begin() as db:
        # The team is a stable parent even before a first baseline exists.
        lock_team_and_require_member(db, team_id=team_id, actor_user_id=user_id)
        run = _lock_owned_run(db, user_id, run_id, team_id=team_id)
        if run.status != "completed":
            raise HTTPException(status_code=409, detail="只有已完成的评测可以存为基线")
        # Explicit SQL order avoids the immediate partial unique index seeing
        # the new true value before ORM flush has cleared the previous row.
        db.execute(
            update(EvalRun).where(
                EvalRun.user_id == user_id,
                EvalRun.team_id == team_id,
                EvalRun.test_set_id == run.test_set_id,
                EvalRun.is_baseline.is_(True),
            ).values(is_baseline=False).execution_options(synchronize_session=False)
        )
        db.execute(
            update(EvalRun).where(EvalRun.id == run_id).values(is_baseline=True)
            .execution_options(synchronize_session=False)
        )
        db.refresh(run)
        result = _run_out(db, run)
    return result


def review_task(user_id: UUID, task_id: UUID, body: EvalReviewInput, *, team_id: UUID) -> EvalTaskOut:
    with sessionLocal.begin() as db:
        lock_team_and_require_member(db, team_id=team_id, actor_user_id=user_id)
        # Fetch only the id before locking the parent; no stale task entity can
        # enter the identity map while waiting for a concurrent review.
        run_id = db.scalar(
            select(EvalTask.run_id).join(EvalRun, EvalRun.id == EvalTask.run_id)
            .where(EvalTask.id == task_id, EvalRun.user_id == user_id, EvalRun.team_id == team_id)
        )
        if run_id is None:
            raise HTTPException(status_code=404, detail="评测任务不存在")
        run = _lock_owned_run(db, user_id, run_id, team_id=team_id)
        task = db.scalar(
            select(EvalTask).where(EvalTask.id == task_id, EvalTask.run_id == run_id).with_for_update()
        )
        if task is None:
            raise HTTPException(status_code=404, detail="评测任务不存在")
        case = db.get(EvalCase, task.case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="评测用例不存在")
        if task.execution_status in {"pending", "running"}:
            raise HTTPException(status_code=409, detail="任务还在执行，不能复核")
        if task.timed_out or task.empty_output or task.execution_status in {"timeout", "error"}:
            raise HTTPException(status_code=409, detail="超时、空输出和报错算失败，不能从统计里改掉")
        scores = dict(task.scores or task.auto_scores or {})
        for key in ("facts", "completeness", "tone", "format"):
            verdict = getattr(body, key)
            if verdict is None:
                continue
            scores[key] = {
                **dict(scores.get(key) or {}),
                "verdict": verdict,
                "reason": body.note,
                "source": "human",
            }
        if any(key not in scores for key in DIMENSIONS):
            raise HTTPException(status_code=409, detail="这项还没有自动评分")
        task.scores = scores
        task.human_note = body.note
        task.qualified = is_qualified(scores)
        task.failure_reasons = failure_reasons(scores)
        flag_modified(task, "scores")
        flag_modified(task, "failure_reasons")
        db.flush()
        if run.status in {"completed", "failed"}:
            refresh_run_artifacts(db, run)
        db.flush()
        result = _task_out(task, case)
    return result


def rebuild_report(user_id: UUID, run_id: UUID, *, team_id: UUID) -> EvalRunOut:
    with sessionLocal.begin() as db:
        lock_team_and_require_member(db, team_id=team_id, actor_user_id=user_id)
        run = _lock_owned_run(db, user_id, run_id, team_id=team_id)
        if run.status not in {"completed", "failed"}:
            raise HTTPException(status_code=409, detail="评测还没结束")
        refresh_run_artifacts(db, run)
        db.flush()
        result = _run_out(db, run)
    return result


def compare_runs(db: Session, user, left_id: UUID, right_id: UUID, *, team_id: UUID) -> EvalCompareOut:
    user_id = _recover_before_read(db, user, team_id=team_id)
    if left_id == right_id:
        raise HTTPException(status_code=400, detail="请选择两次不同的评测")
    left = _owned_run(db, user_id, left_id, team_id=team_id)
    right = _owned_run(db, user_id, right_id, team_id=team_id)
    if left.status != "completed" or right.status != "completed":
        raise HTTPException(status_code=409, detail="只能对比已完成的评测")
    left_version = (left.provenance or {}).get("test_set_version")
    right_version = (right.provenance or {}).get("test_set_version")
    if left_version != right_version:
        raise HTTPException(status_code=409, detail="两次评测的测试集版本不同，不能比较")
    left_map = _tasks_by_code(db, left.id)
    right_map = _tasks_by_code(db, right.id)
    codes = sorted(set(left_map) | set(right_map), key=lambda code: left_map.get(code, right_map[code])[1].ordinal)
    rows = []
    compare_views = []
    for code in codes:
        left_pair = left_map.get(code)
        right_pair = right_map.get(code)
        if left_pair is None or right_pair is None:
            continue
        left_task, case = left_pair
        right_task, _case = right_pair
        rows.append(CompareRow(
            code=case.code,
            title=case.title,
            left=_side(left_task),
            right=_side(right_task),
        ))
        compare_views.append({
            "code": case.code,
            "title": case.title,
            "left_qualified": left_task.qualified,
            "right_qualified": right_task.qualified,
            "left_reasons": list(left_task.failure_reasons or []) or (["合格"] if left_task.qualified else ["无原因"]),
            "right_reasons": list(right_task.failure_reasons or []) or (["合格"] if right_task.qualified else ["无原因"]),
        })
    markdown = render_comparison(
        {"label": left.label, "provenance": left.provenance, "summary": left.summary},
        {"label": right.label, "provenance": right.provenance, "summary": right.summary},
        compare_views,
    )
    counts = _task_counts(db, [left.id, right.id])
    return EvalCompareOut(
        left=_run_out(db, left, counts.get(left.id, (0, 0))),
        right=_run_out(db, right, counts.get(right.id, (0, 0))),
        rows=rows,
        comparison_markdown=markdown,
    )


def _owned_run(db: Session, user_id: UUID, run_id: UUID, *, team_id: UUID) -> EvalRun:
    run = db.scalar(
        select(EvalRun).where(
            EvalRun.id == run_id, EvalRun.user_id == user_id, EvalRun.team_id == team_id,
        ).execution_options(populate_existing=True)
    )
    if run is None:
        raise HTTPException(status_code=404, detail="评测运行不存在")
    return run


def _money(value) -> float | None:
    if value is None:
        return None
    return float(value)


def _task_out(task: EvalTask, case: EvalCase) -> EvalTaskOut:
    return EvalTaskOut(
        id=task.id,
        case_id=case.id,
        code=case.code,
        title=case.title,
        user_request=case.user_request,
        brand_profile=case.brand_profile,
        brand_name=case.brand_name,
        platforms=list(case.platforms or []),
        must_include=list(case.must_include or []),
        forbidden_claims=list(case.forbidden_claims or []),
        execution_status=task.execution_status,
        empty_output=task.empty_output,
        timed_out=task.timed_out,
        revision_count=task.revision_count,
        qualified=task.qualified,
        generated_content=task.generated_content,
        strategy=task.strategy,
        retrieved_context=task.retrieved_context,
        duration_ms=task.duration_ms,
        api_cost_usd=_money(task.api_cost_usd),
        judge_cost_usd=_money(task.judge_cost_usd),
        error=task.error,
        scores=task.scores,
        auto_scores=task.auto_scores,
        human_note=task.human_note,
        failure_reasons=list(task.failure_reasons or []),
    )


def _tasks_by_code(db: Session, run_id: UUID) -> dict[str, tuple[EvalTask, EvalCase]]:
    rows = (
        db.query(EvalTask, EvalCase)
        .join(EvalCase, EvalCase.id == EvalTask.case_id)
        .filter(EvalTask.run_id == run_id)
        .all()
    )
    return {case.code: (task, case) for task, case in rows}


def _side(task: EvalTask) -> CompareSide:
    scores = task.scores or {}
    reasons = list(task.failure_reasons or [])
    if task.qualified and not reasons:
        reasons = ["合格"]
    return CompareSide(
        qualified=bool(task.qualified),
        reasons=reasons,
        facts_verdict=(scores.get("facts") or {}).get("verdict"),
        duration_ms=task.duration_ms,
        api_cost_usd=_money(task.api_cost_usd),
        empty_output=bool(task.empty_output),
        timed_out=bool(task.timed_out),
        execution_status=task.execution_status,
    )

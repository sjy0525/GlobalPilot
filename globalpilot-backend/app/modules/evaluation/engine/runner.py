"""跑工作流的检索、调研和初稿，不进入自审返工。"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime
from uuid import UUID

from langchain_core.runnables.config import var_child_runnable_config
from sqlalchemy import select

from app.modules.evaluation.engine.judge import judge_copy
from app.modules.evaluation.engine.lease import (
    HEARTBEAT_SECONDS,
    LeaseLost,
    LeaseToken,
    clear_locked_slot,
    lock_owned_lease,
    lock_slot,
    renew_lease,
)
from app.modules.evaluation.engine.persistence import _mark_run_failed, recover_expired_run, refresh_run_artifacts, task_metrics
from app.modules.evaluation.engine.scoring import blocked_scores, failure_reasons, is_empty_content, is_qualified, score_draft
from app.modules.evaluation.engine.usage import UsageHandler, invoke_config
from app.db.database import sessionLocal
from app.modules.brands.models import Brand
from app.modules.evaluation.models import EvalCase, EvalRun, EvalTask
from app.modules.marketing.workflow.nodes import (
    NO_KNOWLEDGE,
    generate_content,
    generate_strategy,
    research_node,
    retrieve_context,
)

logger = logging.getLogger(__name__)

GENERATION_TIMEOUT_SECONDS = 180
JUDGE_TIMEOUT_SECONDS = 90


def launch(token: LeaseToken) -> None:
    threading.Thread(target=_guard, args=(token,), name=f"eval-{token.run_id}", daemon=True).start()


def _heartbeat(token: LeaseToken, stop: threading.Event, lost: threading.Event) -> None:
    while not stop.wait(HEARTBEAT_SECONDS):
        try:
            with sessionLocal.begin() as db:
                renew_lease(db, token)
        except Exception:
            # Fail closed: a paused or disconnected executor cannot resurrect
            # its lease. Every later business write also checks it in the DB.
            lost.set()
            logger.warning("Eval heartbeat stopped for %s", token.run_id, exc_info=True)
            return


def _guard(token: LeaseToken) -> None:
    stop = threading.Event()
    lost = threading.Event()
    heartbeat = threading.Thread(
        target=_heartbeat, args=(token, stop, lost), name=f"eval-heartbeat-{token.run_id}", daemon=True,
    )
    try:
        heartbeat.start()
        execute(token, lost)
    except LeaseLost:
        logger.info("Discarding an expired eval executor: %s", token.run_id)
    except Exception:
        logger.exception("Eval run failed")
        try:
            _fail_run(token, "评测进程异常退出")
        except LeaseLost:
            logger.info("Eval failure belongs to an expired executor: %s", token.run_id)
        except Exception:
            logger.exception("Could not persist eval failure; lease recovery will handle it")
    finally:
        stop.set()
        if heartbeat.ident is not None:
            heartbeat.join(timeout=2)


def _locked_run(db, token: LeaseToken, *, authorize: bool = True):
    if authorize:
        from app.modules.teams.authorization import lock_team_and_require_member

        scope = db.execute(
            select(EvalRun.team_id, EvalRun.user_id).where(EvalRun.id == token.run_id)
        ).one_or_none()
        if scope is None or scope.team_id is None:
            raise RuntimeError("评测运行尚未确定团队归属")
        # Match API writes' team -> executor lease -> run lock order. Release
        # all locks before provider IO, then recheck before publishing output.
        lock_team_and_require_member(
            db, team_id=scope.team_id, actor_user_id=scope.user_id,
        )
    lease = lock_owned_lease(db, token)
    run = db.scalar(
        select(EvalRun).where(EvalRun.id == token.run_id).with_for_update()
        .execution_options(populate_existing=True)
    )
    if run is None or run.status not in {"queued", "running"}:
        raise LeaseLost("评测已结束")
    return lease, run


def execute(token: LeaseToken, lost: threading.Event | None = None) -> None:
    with sessionLocal.begin() as db:
        _lease, run = _locked_run(db, token)
        if run.status != "queued":
            return
        run.status = "running"
        run.started_at = datetime.now()
        task_ids = list(db.scalars(select(EvalTask.id).where(EvalTask.run_id == run.id)))

    for task_id in task_ids:
        if lost is not None and lost.is_set():
            raise LeaseLost("评测心跳已停止")
        try:
            _execute_task(token, task_id)
        except LeaseLost:
            raise
        except Exception:
            logger.exception("Eval task failed")
            _fail_task(token, task_id, "任务执行异常")
    _finalize(token)


def generate_first_draft(state: dict, handler: UsageHandler) -> dict:
    config = invoke_config(handler)
    token = var_child_runnable_config.set(config)
    try:
        retrieved = retrieve_context(state)
        merged = {**state, **retrieved}
        context = merged.get("retrieved_context") or merged.get("context") or ""
        if not str(context).strip():
            return {
                "content": None,
                "strategy": None,
                "retrieved": "",
                "empty": True,
                "blocked": NO_KNOWLEDGE,
            }
        merged.update(research_node(merged, config=config))
        strategy_update = generate_strategy(merged)
        merged.update(strategy_update)
        content_update = generate_content(merged)
        content = content_update.get("content")
        empty = is_empty_content(content)
        return {
            "content": content,
            "strategy": strategy_update.get("strategy"),
            "retrieved": context,
            "empty": empty,
            "blocked": "空输出" if empty else None,
        }
    finally:
        var_child_runnable_config.reset(token)


def _execute_task(token: LeaseToken, task_id: UUID) -> None:
    with sessionLocal.begin() as db:
        _lease, run = _locked_run(db, token)
        task = db.scalar(
            select(EvalTask).where(EvalTask.id == task_id, EvalTask.run_id == token.run_id).with_for_update()
        )
        case = db.get(EvalCase, task.case_id) if task else None
        if task is None or case is None:
            raise RuntimeError("评测任务或用例不存在")
        if task.execution_status != "pending":
            return
        task.execution_status = "running"
        brand = db.query(Brand).filter(
            Brand.name == case.brand_name, Brand.team_id == run.team_id,
        ).one_or_none()
        case_payload = {
            "user_request": case.user_request,
            "brand_profile": case.brand_profile,
            "must_include": list(case.must_include or []),
            "forbidden_claims": list(case.forbidden_claims or []),
            "platforms": list(case.platforms or []),
            "region": case.region,
            "persona": case.persona,
            "purchase_channels": case.purchase_channels,
            "brand_id": brand.id if brand else None,
            "team_id": run.team_id,
            "actor_user_id": run.user_id,
        }
    outcome = _run_case(case_payload)

    with sessionLocal.begin() as db:
        _locked_run(db, token)
        task = db.scalar(
            select(EvalTask).where(EvalTask.id == task_id, EvalTask.run_id == token.run_id).with_for_update()
        )
        if task is None or task.execution_status != "running":
            raise LeaseLost("评测任务已结束")
        _apply_outcome(task, outcome)


def _run_case(case: dict) -> dict:
    started = time.perf_counter()
    handler = UsageHandler()
    if case["brand_id"] is None:
        return _blocked_outcome("评测品牌不存在", started, handler)
    state = {
        "brand_id": case["brand_id"],
        "team_id": case["team_id"],
        "actor_user_id": case["actor_user_id"],
        "goal": case["user_request"],
        "selling_points": "；".join(case["must_include"]),
        "region": case["region"],
        "user_persona": case["persona"],
        "social_media": case["platforms"],
        "purchase_channels": case["purchase_channels"],
        "competitor_url": None,
    }
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(generate_first_draft, state, handler)
        try:
            draft = future.result(timeout=GENERATION_TIMEOUT_SECONDS)
        except FuturesTimeout:
            return _blocked_outcome("生成超时", started, handler, timed_out=True)
        except Exception as exc:
            logger.exception("First draft failed")
            return _blocked_outcome(str(exc) or "生成失败", started, handler, execution_status="error")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    duration_ms = int((time.perf_counter() - started) * 1000)
    usage = handler.snapshot()
    if draft.get("blocked"):
        scores = blocked_scores(draft["blocked"])
        return {
            "execution_status": "completed",
            "empty_output": True,
            "timed_out": False,
            "qualified": False,
            "content": draft.get("content"),
            "strategy": draft.get("strategy"),
            "retrieved": draft.get("retrieved") or "",
            "duration_ms": duration_ms,
            "usage": usage,
            "api_cost_usd": usage["usd"],
            "judge_cost_usd": None,
            "error": draft["blocked"],
            "scores": scores,
        }

    judge, judge_usage, judge_error = _judge(case, draft)
    if judge is None:
        reason = judge_error or "评测模型没有返回结果"
        judge = {
            key: {"verdict": "pending", "reason": reason}
            for key in ("facts", "completeness", "tone")
        }
    scores = score_draft(draft.get("content"), case["platforms"], judge)
    return {
        "execution_status": "completed",
        "empty_output": False,
        "timed_out": False,
        "qualified": is_qualified(scores),
        "content": draft.get("content"),
        "strategy": draft.get("strategy"),
        "retrieved": draft.get("retrieved") or "",
        "duration_ms": duration_ms,
        "usage": {"generation": usage, "judge": judge_usage},
        "api_cost_usd": usage["usd"],
        "judge_cost_usd": None if judge_usage is None else judge_usage.get("usd"),
        "error": None,
        "scores": scores,
    }


def _judge(case: dict, draft: dict) -> tuple[dict | None, dict | None, str | None]:
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(
            judge_copy,
            brand_profile=case["brand_profile"],
            retrieved_context=draft.get("retrieved") or "",
            user_request=case["user_request"],
            must_include=case["must_include"],
            forbidden_claims=case["forbidden_claims"],
            content=draft.get("content") or {},
        )
        try:
            verdict, usage = future.result(timeout=JUDGE_TIMEOUT_SECONDS)
        except FuturesTimeout:
            return None, None, "评测模型超时"
        except Exception as exc:
            logger.exception("Judge crashed")
            return None, None, str(exc) or "评测模型失败"
        if verdict is None:
            return None, usage, "评测模型输出无法解析"
        return verdict, usage, None
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _blocked_outcome(
    reason: str,
    started: float,
    handler: UsageHandler,
    *,
    timed_out: bool = False,
    execution_status: str = "error",
) -> dict:
    usage = handler.snapshot()
    status = "timeout" if timed_out else execution_status
    scores = blocked_scores(reason)
    return {
        "execution_status": status,
        "empty_output": False,
        "timed_out": timed_out,
        "qualified": False,
        "content": None,
        "strategy": None,
        "retrieved": "",
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "usage": usage,
        "api_cost_usd": usage["usd"],
        "judge_cost_usd": None,
        "error": reason,
        "scores": scores,
    }


def _apply_outcome(task: EvalTask, outcome: dict) -> None:
    scores = outcome["scores"]
    task.execution_status = outcome["execution_status"]
    task.empty_output = outcome["empty_output"]
    task.timed_out = outcome["timed_out"]
    task.revision_count = 0
    task.qualified = outcome["qualified"]
    task.generated_content = outcome["content"]
    task.strategy = outcome["strategy"]
    task.retrieved_context = outcome["retrieved"]
    task.duration_ms = outcome["duration_ms"]
    task.api_cost_usd = outcome["api_cost_usd"]
    task.judge_cost_usd = outcome["judge_cost_usd"]
    task.usage = outcome["usage"]
    task.error = outcome["error"]
    task.scores = scores
    task.auto_scores = scores
    task.failure_reasons = failure_reasons(scores)


def _fail_task(token: LeaseToken, task_id: UUID, reason: str) -> None:
    with sessionLocal.begin() as db:
        _locked_run(db, token)
        task = db.scalar(
            select(EvalTask).where(EvalTask.id == task_id, EvalTask.run_id == token.run_id).with_for_update()
        )
        if task is None or task.execution_status in {"completed", "timeout", "error"}:
            return
        scores = blocked_scores(reason)
        task.execution_status = "error"
        task.empty_output = True
        task.qualified = False
        task.error = reason
        task.scores = scores
        task.auto_scores = scores
        task.failure_reasons = failure_reasons(scores)


def _fail_run(token: LeaseToken, reason: str) -> None:
    with sessionLocal.begin() as db:
        # Cleanup must still release an owned lease after membership removal.
        lease, run = _locked_run(db, token, authorize=False)
        _mark_run_failed(db, run, reason)
        clear_locked_slot(db, lease)


def recover_expired_runs() -> None:
    with sessionLocal.begin() as db:
        recover_expired_run(db, lock_slot(db))


def _finalize(token: LeaseToken) -> None:
    with sessionLocal.begin() as db:
        lease, run = _locked_run(db, token)
        pending = db.scalar(
            select(EvalTask.id).where(
                EvalTask.run_id == run.id, EvalTask.execution_status.in_(("pending", "running")),
            ).limit(1)
        )
        if pending is not None:
            raise RuntimeError("评测任务尚未完成")
        refresh_run_artifacts(db, run)
        run.status = "completed"
        run.finished_at = datetime.now()
        clear_locked_slot(db, lease)

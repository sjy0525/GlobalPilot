"""初稿合格率的分母是全部任务，超时和空输出留在统计里。"""
from dataclasses import dataclass, field


@dataclass
class TaskMetric:
    case_code: str
    title: str
    qualified: bool
    revision_count: int
    facts_verdict: str
    execution_status: str
    empty_output: bool
    timed_out: bool
    duration_ms: int | None
    api_cost_usd: float | None
    failure_reasons: list[str] = field(default_factory=list)


def first_draft_pass_rate(tasks: list[TaskMetric]) -> dict:
    total = len(tasks)
    passed = sum(1 for task in tasks if task.qualified and task.revision_count == 0)
    return {
        "passed": passed,
        "total": total,
        "rate": (passed / total) if total else None,
    }


def is_fact_error(task: TaskMetric) -> bool:
    return (
        task.execution_status == "completed"
        and not task.empty_output
        and not task.timed_out
        and task.facts_verdict == "fail"
    )


def summarize(tasks: list[TaskMetric]) -> dict:
    rate = first_draft_pass_rate(tasks)
    durations = [task.duration_ms for task in tasks if task.duration_ms is not None]
    costs = [task.api_cost_usd for task in tasks if task.api_cost_usd is not None]
    qualified = rate["passed"]
    total_cost = round(sum(costs), 8) if costs else 0.0
    return {
        "total": rate["total"],
        "first_draft_passed": qualified,
        "first_draft_pass_rate": rate["rate"],
        "qualified_count": qualified,
        "fact_error_count": sum(1 for task in tasks if is_fact_error(task)),
        "fact_pending_count": sum(
            1
            for task in tasks
            if task.execution_status == "completed"
            and not task.empty_output
            and not task.timed_out
            and task.facts_verdict == "pending"
        ),
        "empty_count": sum(1 for task in tasks if task.empty_output),
        "timeout_count": sum(1 for task in tasks if task.timed_out),
        "error_count": sum(1 for task in tasks if task.execution_status == "error"),
        "duration_ms_total": sum(durations),
        "duration_ms_avg": (sum(durations) / len(durations)) if durations else None,
        "api_cost_usd_total": total_cost,
        "cost_per_qualified_usd": (total_cost / qualified) if qualified else None,
        "cost_samples": len(costs),
    }

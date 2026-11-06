"""Task-scoped revision graph with durable clarification and review interrupts."""

from functools import partial

from langgraph.graph import END, START, StateGraph

from app.modules.marketing.workflow.revision.agent import (
    RevisionReviser,
    invoke_revision_reviser,
    invoke_semantic_reviewer,
)
from app.modules.marketing.workflow.revision import nodes, tools
from app.modules.marketing.workflow.revision.state import RevisionState
from app.modules.marketing.workflow.revision.validation import SemanticReviewer


def build_revision_graph(
    *,
    reviewer: SemanticReviewer = invoke_semantic_reviewer,
    reviser: RevisionReviser = invoke_revision_reviser,
    max_revisions: int = 0,
    interactive: bool = False,
    checkpointer=None,
    document_reader: tools.DocumentReader | None = None,
    evidence_searcher: tools.EvidenceSearcher | None = None,
):
    """One-shot callers stay compatible; business runs explicitly opt in.

    Interactive callers provide an authorization-fenced persistent checkpointer
    and task-bound read ports. InMemorySaver is only appropriate for tests.
    """
    if type(max_revisions) is not int or max_revisions < 0:
        raise ValueError("max_revisions 必须是非负整数")
    builder = StateGraph(RevisionState)
    if not interactive and max_revisions == 0:
        builder.add_node("validate_candidate", partial(nodes.validate_candidate_node, reviewer=reviewer))
        builder.add_edge(START, "validate_candidate")
        builder.add_edge("validate_candidate", END)
        return builder.compile(checkpointer=checkpointer)

    if interactive:
        if checkpointer is None or document_reader is None or evidence_searcher is None:
            raise ValueError("交互改稿需要持久化检查点与授权读取工具")
        builder.add_node("read_document", partial(nodes.read_document_node, reader=document_reader))
        builder.add_node("search_brand_evidence", partial(nodes.load_evidence_node, searcher=evidence_searcher))
        builder.add_node("clarification", nodes.clarification_node)
        builder.add_node("human_review", nodes.human_review_node)
        builder.add_edge(START, "read_document")
        builder.add_edge("read_document", "search_brand_evidence")
    else:
        builder.add_node("initialize", nodes.initialize_supplied_node)
        builder.add_edge(START, "initialize")

    builder.add_node("generate_candidate", partial(nodes.generate_candidate_node, reviser=reviser))
    builder.add_node("validate_candidate", partial(nodes.check_revision_node, reviewer=reviewer))
    builder.add_node("revise_candidate", partial(nodes.revise_candidate_node, reviser=reviser, max_revisions=max_revisions))
    builder.add_node("budget_exhausted", nodes.exhaust_budget_node)
    builder.add_edge("budget_exhausted", END)

    def route_loaded(state):
        if state.get("status") == "needs_clarification":
            return "clarify" if interactive else "end"
        return "check" if state.get("candidate") else "generate"

    loaded_routes = {"check": "validate_candidate", "generate": "generate_candidate", "end": END}
    if interactive:
        loaded_routes["clarify"] = "clarification"
    builder.add_conditional_edges(
        "search_brand_evidence" if interactive else "initialize", route_loaded, loaded_routes,
    )
    generation_routes = {"end": END, "check": "validate_candidate"}
    if interactive:
        generation_routes["clarify"] = "clarification"

    def after_generation(state):
        if state.get("status") in {"error", "budget_exhausted"}:
            return "end"
        if state.get("status") == "needs_clarification":
            return "clarify" if interactive else "end"
        return "check"

    builder.add_conditional_edges("generate_candidate", after_generation, generation_routes)

    def after_check(state):
        status = state["semantic_validation"]["status"]
        if status == "passed":
            return "review" if interactive else "end"
        if status == "needs_clarification":
            return "clarify" if interactive else "end"
        if status == "error":
            return "end"
        return "repair" if state.get("revision_attempts", 0) < max_revisions else "exhausted"

    routes = {"repair": "revise_candidate", "exhausted": "budget_exhausted", "end": END}
    if interactive:
        routes.update(review="human_review", clarify="clarification")
    builder.add_conditional_edges("validate_candidate", after_check, routes)
    builder.add_conditional_edges("revise_candidate", after_generation, generation_routes)
    if interactive:
        builder.add_conditional_edges("clarification", route_loaded, loaded_routes)
        builder.add_edge("human_review", END)
    return builder.compile(checkpointer=checkpointer)

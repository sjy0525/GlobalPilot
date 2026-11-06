from langgraph.graph import END, START, StateGraph

from app.modules.marketing.workflow.checkpointer import checkpointer
from app.modules.marketing.workflow.nodes import (
    final_output,
    generate_content,
    generate_strategy,
    human_review,
    max_revisions,
    no_context,
    research_node,
    retrieve_context,
    self_review_exhausted,
)
from app.modules.marketing.workflow.state import (
    MarketingInputState,
    MarketingOutputState,
    MarketingState,
)

from app.modules.marketing.workflow.self_review import review_passed, self_review

# 评测快照会记下这个版本。改图的节点或路由时要一起改。
WORKFLOW_VERSION = "marketing-workflow-v3-memory-feedback"

MAX_REVISIONS = 3
MAX_SELF_REVIEW_ATTEMPTS = 3

builder = StateGraph(
    MarketingState,
    input_schema=MarketingInputState,
    output_schema=MarketingOutputState,
)

builder.add_node("retrieve_context", retrieve_context)
builder.add_node("no_context", no_context)
builder.add_node("research_agent", research_node)
builder.add_node("generate_strategy", generate_strategy)
builder.add_node("generate_content", generate_content)
builder.add_node("self_review", self_review)
builder.add_node("human_review", human_review)
builder.add_node("final_output", final_output)
builder.add_node("max_revisions", max_revisions)
builder.add_node("self_review_exhausted", self_review_exhausted)


def route_after_retrieve(state: MarketingState) -> str:
    return "found" if state.get("context") else "not_found"


def revisions_exhausted(revision_count: int) -> bool:
    return revision_count >= MAX_REVISIONS


def route_after_self_review(state: MarketingState) -> str:
    if review_passed(state):
        return "passed"
    attempts = int(state.get("self_review_attempts") or 0)
    if attempts >= MAX_SELF_REVIEW_ATTEMPTS:
        return "exhausted"
    return "revise"


def route_after_review(state: MarketingState) -> str:
    if state.get("decision") == "approved":
        return "approved"
    if revisions_exhausted(state.get("revision_count", 0) or 0):
        return "exhausted"
    return "revise"


builder.add_edge(START, "retrieve_context")
builder.add_conditional_edges(
    "retrieve_context",
    route_after_retrieve,
    {
        "found": "research_agent",
        "not_found": "no_context",
    },
)
builder.add_edge("research_agent", "generate_strategy")
builder.add_edge("generate_strategy", "generate_content")
builder.add_edge("generate_content", "self_review")
builder.add_conditional_edges(
    "self_review",
    route_after_self_review,
    {
        "passed": "human_review",
        "revise": "generate_content",
        "exhausted": "self_review_exhausted",
    },
)
builder.add_conditional_edges(
    "human_review",
    route_after_review,
    {
        "approved": "final_output",
        "revise": "generate_strategy",
        "exhausted": "max_revisions",
    },
)
builder.add_edge("final_output", END)
builder.add_edge("no_context", END)
builder.add_edge("max_revisions", END)
builder.add_edge("self_review_exhausted", END)

graph = builder.compile(checkpointer=checkpointer)

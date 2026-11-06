import { ApiError, apiJson, apiNdjson } from "./http";
import type { Brief } from "./briefs";

export type WorkflowStatus =
  | "no_knowledge"
  | "awaiting_review"
  | "completed"
  | "max_revisions"
  | "self_review_failed";

export type GraphNode =
  | "retrieve_context"
  | "no_context"
  | "research_agent"
  | "generate_strategy"
  | "generate_content"
  | "self_review"
  | "human_review"
  | "final_output"
  | "max_revisions"
  | "self_review_exhausted";

export type ContentReview = {
  status: "passed" | "needs_revision" | "error";
  rules_version: string;
  content_hash: string;
  errors: string[];
  facts: { claim: string; verdict: "supported" | "unsupported" | "contradicted"; evidence: string; suggestion: string }[];
  blockers?: string[];
  summary?: string;
  attempts?: number;
  stuck_at?: string | null;
};

export type PlatformCopy = {
  hook: string;
  body: string;
  cta: string;
  tags: string;
};

export type MarketingCopy = {
  version: string;
  platforms: Record<string, PlatformCopy>;
};

export type ResearchReport = {
  status: "not_needed" | "completed" | "insufficient";
  summary: string;
  source_urls: string[];
  uncertainties: string[];
};

export type ResearchValidation = {
  passed: boolean;
  issues?: string[];
};

export type MarketingWorkflow = {
  self_review?: ContentReview | null;
  research?: ResearchReport | null;
  research_validation?: ResearchValidation | null;
  brief_id: string;
  checkpoint_id: string | null;
  status: WorkflowStatus;
  revision_count: number;
  content: MarketingCopy | null;
  strategy: string | null;
  final_output: string | null;
};

export type ReviewAction = "approved" | "rejected";

export type WorkflowStreamEvent =
  | {
      type: "node";
      node: GraphNode;
      phase: "running" | "done";
      strategy?: string | null;
      content?: MarketingCopy | null;
      research?: ResearchReport | null;
      research_validation?: ResearchValidation | null;
    }
  | { type: "done"; workflow: MarketingWorkflow }
  | { type: "error"; detail: string; status?: number; code?: string };

const GRAPH_NODES: GraphNode[] = [
  "retrieve_context",
  "no_context",
  "research_agent",
  "generate_strategy",
  "generate_content",
  "self_review",
  "human_review",
  "final_output",
  "max_revisions",
  "self_review_exhausted",
];

type StartInflight = {
  promise: Promise<MarketingWorkflow>;
  listeners: Set<(event: WorkflowStreamEvent) => void>;
};

const startInflight = new Map<string, StartInflight>();

function workflowPath(path: string, teamId?: string) {
  if (!teamId) return path;
  return `${path}${path.includes('?') ? '&' : '?'}team_id=${encodeURIComponent(teamId)}`;
}

export function isGraphNode(value: unknown): value is GraphNode {
  return typeof value === "string" && GRAPH_NODES.includes(value as GraphNode);
}

function isPlatformCopy(value: unknown): value is PlatformCopy {
  if (!value || typeof value !== "object") return false;
  const item = value as Record<string, unknown>;
  return ["hook", "body", "cta", "tags"].every((key) => typeof item[key] === "string");
}

export function isMarketingCopy(value: unknown): value is MarketingCopy {
  if (!value || typeof value !== "object") return false;
  const platforms = (value as { platforms?: unknown }).platforms;
  if (!platforms || typeof platforms !== "object" || Array.isArray(platforms)) return false;
  return Object.values(platforms).every(isPlatformCopy);
}

export function hasMarketingCopy(value: MarketingCopy | null | undefined): boolean {
  return Boolean(value && Object.keys(value.platforms).length > 0);
}

function isResearchReport(value: unknown): value is ResearchReport {
  if (!value || typeof value !== "object") return false;
  const report = value as Record<string, unknown>;
  return ["not_needed", "completed", "insufficient"].includes(String(report.status)) &&
    typeof report.summary === "string" &&
    Array.isArray(report.source_urls) && report.source_urls.every(url => typeof url === "string") &&
    Array.isArray(report.uncertainties) && report.uncertainties.every(item => typeof item === "string");
}

function isResearchValidation(value: unknown): value is ResearchValidation {
  if (!value || typeof value !== "object") return false;
  const validation = value as Record<string, unknown>;
  return typeof validation.passed === "boolean" &&
    (validation.issues === undefined || (Array.isArray(validation.issues) &&
      validation.issues.every(item => typeof item === "string")));
}

function isWorkflow(value: unknown): value is MarketingWorkflow {
  if (!value || typeof value !== "object") return false;
  const record = value as Partial<MarketingWorkflow>;
  return typeof record.brief_id === "string" && typeof record.status === "string";
}

function parseStreamEvent(value: unknown): WorkflowStreamEvent | null {
  if (!value || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  if (record.type === "node" && isGraphNode(record.node)) {
    return {
      type: "node",
      node: record.node,
      phase: record.phase === "done" ? "done" : "running",
      strategy: typeof record.strategy === "string" ? record.strategy : null,
      content: isMarketingCopy(record.content) ? record.content : null,
      research: isResearchReport(record.research) ? record.research : null,
      research_validation: isResearchValidation(record.research_validation) ? record.research_validation : null,
    };
  }
  if (record.type === "done" && isWorkflow(record.workflow)) {
    return { type: "done", workflow: record.workflow };
  }
  if (record.type === "error") {
    return {
      type: "error",
      detail:
        typeof record.detail === "string" && record.detail.trim()
          ? record.detail
          : "Workflow failed.",
      status:
        typeof record.status === "number" &&
        Number.isInteger(record.status) &&
        record.status >= 400 &&
        record.status <= 599
          ? record.status
          : 500,
      code: typeof record.code === "string" ? record.code : undefined,
    };
  }
  return null;
}

async function consumeWorkflowStream(
  path: string,
  body: unknown,
  onEvent?: (event: WorkflowStreamEvent) => void,
): Promise<MarketingWorkflow> {
  let result: MarketingWorkflow | null = null;
  await apiNdjson(path, { method: "POST", body: JSON.stringify(body) }, (value) => {
    if (isWorkflow(value) && !("type" in (value as object))) {
      result = value;
      return;
    }
    const event = parseStreamEvent(value);
    if (!event) return;
    onEvent?.(event);
    if (event.type === "done") result = event.workflow;
    if (event.type === "error") {
      throw new ApiError(event.detail, event.status ?? 500, event.code);
    }
  });
  if (!result) {
    throw new ApiError("Stream ended without a result.", 500);
  }
  return result;
}

export function startMarketingWorkflow(briefId: string, teamId?: string) {
  return apiJson<MarketingWorkflow>(workflowPath("/api/briefs/workflow", teamId), {
    method: "POST",
    body: JSON.stringify({ brief_id: briefId }),
  });
}

export function getMarketingWorkflow(briefId: string, teamId?: string) {
  return apiJson<MarketingWorkflow>(
    workflowPath(`/api/briefs/workflow?brief_id=${encodeURIComponent(briefId)}`, teamId),
  );
}

export function reviewMarketingWorkflow(
  briefId: string,
  action: ReviewAction,
  expectedCheckpointId: string,
  feedback?: string | null,
  teamId?: string,
) {
  return apiJson<MarketingWorkflow>(workflowPath("/api/briefs/workflow/review", teamId), {
    method: "POST",
    body: JSON.stringify({
      brief_id: briefId,
      action,
      expected_checkpoint_id: expectedCheckpointId,
      feedback: feedback?.trim() || null,
    }),
  });
}

export function streamStartWorkflow(
  briefId: string,
  onEvent?: (event: WorkflowStreamEvent) => void,
  teamId?: string,
) {
  return consumeWorkflowStream(
    workflowPath("/api/briefs/workflow/stream", teamId),
    { brief_id: briefId },
    onEvent,
  );
}

export function streamReviewWorkflow(
  briefId: string,
  action: ReviewAction,
  expectedCheckpointId: string,
  feedback: string | null,
  onEvent?: (event: WorkflowStreamEvent) => void,
  teamId?: string,
) {
  return consumeWorkflowStream(
    workflowPath("/api/briefs/workflow/review/stream", teamId),
    {
      brief_id: briefId,
      action,
      expected_checkpoint_id: expectedCheckpointId,
      feedback: feedback?.trim() || null,
    },
    onEvent,
  );
}

function startMarketingWorkflowOnce(
  briefId: string,
  onEvent?: (event: WorkflowStreamEvent) => void,
  teamId?: string,
) {
  const requestKey = `${teamId ?? ''}:${briefId}`;
  const existing = startInflight.get(requestKey);
  if (existing) {
    if (onEvent) existing.listeners.add(onEvent);
    return existing.promise;
  }
  const listeners = new Set<(event: WorkflowStreamEvent) => void>();
  if (onEvent) listeners.add(onEvent);
  const promise = streamStartWorkflow(briefId, (event) => {
    listeners.forEach((listener) => listener(event));
  }, teamId)
    .catch((err) => {
      if (
        err instanceof ApiError &&
        (err.status === 404 || err.status === 405) &&
        /not found/i.test(err.message)
      ) {
        return startMarketingWorkflow(briefId, teamId);
      }
      throw err;
    })
    .finally(() => {
      startInflight.delete(requestKey);
    });
  startInflight.set(requestKey, { promise, listeners });
  return promise;
}

/** Restore a saved run, or start generation if this brief has no snapshot yet. */
export async function loadOrStartWorkflow(
  briefId: string,
  onEvent?: (event: WorkflowStreamEvent) => void,
  onStart?: () => void,
  teamId?: string,
  isCurrent?: () => boolean,
) {
  try {
    return await getMarketingWorkflow(briefId, teamId);
  } catch (err) {
    if (err instanceof ApiError && (err.status === 404 || err.status === 409)) {
      if (isCurrent && !isCurrent()) throw new ApiError('稿件读取已取消。', 0);
      onStart?.();
      return startMarketingWorkflowOnce(briefId, onEvent, teamId);
    }
    throw err;
  }
}

export async function reviewOrStreamWorkflow(
  briefId: string,
  action: ReviewAction,
  expectedCheckpointId: string,
  feedback: string | null,
  onEvent?: (event: WorkflowStreamEvent) => void,
  teamId?: string,
  isCurrent?: () => boolean,
) {
  try {
    return await streamReviewWorkflow(briefId, action, expectedCheckpointId, feedback, onEvent, teamId);
  } catch (err) {
    if (err instanceof ApiError && err.status === 409) throw err;
    if (
      err instanceof ApiError &&
      (err.status === 404 || err.status === 405) &&
      /not found/i.test(err.message)
    ) {
      if (isCurrent && !isCurrent()) throw new ApiError('稿件审核已取消。', 0);
      return reviewMarketingWorkflow(briefId, action, expectedCheckpointId, feedback, teamId);
    }
    throw err;
  }
}

export function isBriefLike(value: unknown): value is Brief {
  if (!value || typeof value !== "object") return false;
  const record = value as Partial<Brief>;
  return typeof record.id === "string" && typeof record.goal === "string";
}

export const LAST_BRIEF_STORAGE_KEY = "gp-last-brief";

export function readLastBrief(): Brief | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.sessionStorage.getItem(LAST_BRIEF_STORAGE_KEY);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    return isBriefLike(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

export function persistLastBrief(brief: Brief) {
  window.sessionStorage.setItem(LAST_BRIEF_STORAGE_KEY, JSON.stringify(brief));
}

export function clearLastBrief() {
  window.sessionStorage.removeItem(LAST_BRIEF_STORAGE_KEY);
}

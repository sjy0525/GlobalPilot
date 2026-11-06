import { apiJson } from "./http";

export type Region = "eu-na" | "sea" | "jp-kr" | "other";

export const SOCIAL_PLATFORMS = [
  "tiktok",
  "instagram",
  "youtube",
  "linkedin",
  "facebook",
  "x",
] as const;

export type SocialPlatform = (typeof SOCIAL_PLATFORMS)[number];

export type BriefPayload = {
  brand_id: string;
  goal: string;
  selling_points: string;
  region: Region;
  user_persona: string;
  social_media: SocialPlatform[];
  purchase_channels?: string | null;
  competitor_url?: string | null;
};

export type Brief = {
  id: string;
  team_id?: string;
  user_id: string;
  brand_id: string;
  created_at: string;
  updated_at: string;
  goal: string;
  selling_points: string;
  region: Region;
  user_persona: string;
  social_media: SocialPlatform[];
  purchase_channels: string | null;
  competitor_url: string | null;
};

export type BriefListItem = Brief & {
  workflow_status:
    | "no_knowledge"
    | "awaiting_review"
    | "completed"
    | "max_revisions"
    | "self_review_failed";
  revision_count: number;
};

export function createBrief(payload: BriefPayload, teamId?: string) {
  const path = teamId ? `/api/briefs?team_id=${encodeURIComponent(teamId)}` : '/api/briefs';
  return apiJson<Brief>(path, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function listBriefs(teamId?: string, signal?: AbortSignal) {
  const path = teamId ? `/api/briefs?team_id=${encodeURIComponent(teamId)}` : '/api/briefs';
  return apiJson<BriefListItem[]>(path, { signal });
}

import { apiBlob, apiForm, apiJson } from "./http";

export type HardwareBrand = {
  id: string;
  team_id?: string;
  name: string;
  created_at: string;
};

export type BrandDocument = {
  id: string;
  brand_id: string;
  source: string;
  chunk_count: number;
  version: number;
  created_at: string;
  updated_at: string;
};

export type BrandQuerySource = {
  source: string;
  document_id: string;
  chunk_index: number;
  distance: number;
  content: string;
  content_id?: string;
  content_ids?: string[];
  document_version?: number;
  page_no?: number | null;
  page_numbers?: number[];
  kind?: string;
  provenance?: ContentProvenance[];
  evidence?: DocumentEvidence[];
};

export type ContentProvenance = {
  page_no: number;
  bbox?: { l: number; t: number; r: number; b: number };
  coord_origin?: "TOPLEFT" | "BOTTOMLEFT";
  page_width?: number;
  page_height?: number;
};

export type DocumentContent = {
  content_id: string;
  kind: "heading" | "text" | "table" | "picture";
  text: string;
  markdown?: string;
  section_path?: string[];
  page_no?: number | null;
  provenance?: ContentProvenance[];
  table?: Record<string, unknown>;
  asset_id?: string;
  description?: string;
  description_is_generated?: boolean;
  description_source?: string;
  ocr_text?: string;
  caption?: string;
  context_text?: string;
  related_content_ids?: string[];
};

export type DocumentEvidence = DocumentContent & {
  document_id: string;
  document_version: number;
};

export type DocumentStructure = {
  document_id: string;
  version: number;
  source: string;
  structured: {
    schema_version: number;
    parser: string;
    parser_version?: string;
    page_count: number;
    pages: { page_no: number; width: number; height: number }[];
    contents: DocumentContent[];
  } | null;
};

export type BrandQueryResult = {
  answer: string;
  sources: BrandQuerySource[];
};

export function listHardwareBrands(teamId?: string, signal?: AbortSignal) {
  const path = teamId ? `/api/brands?team_id=${encodeURIComponent(teamId)}` : "/api/brands";
  return apiJson<HardwareBrand[]>(path, { signal });
}

function documentPath(path: string, brandId: string, teamId?: string) {
  const query = new URLSearchParams({ brand_id: brandId });
  if (teamId) query.set("team_id", teamId);
  return `${path}?${query}`;
}

export function listBrandDocuments(brandId: string, teamId?: string) {
  return apiJson<BrandDocument[]>(documentPath("/api/documents", brandId, teamId));
}

export function fetchBrandDocumentFile(brandId: string, id: string, teamId?: string, expectedVersion?: number) {
  const path = documentPath(`/api/documents/${encodeURIComponent(id)}/file`, brandId, teamId);
  return apiBlob(`${path}${expectedVersion ? `&expected_version=${expectedVersion}` : ""}`);
}

export function fetchDocumentStructure(brandId: string, id: string, teamId?: string) {
  return apiJson<DocumentStructure>(documentPath(`/api/documents/${encodeURIComponent(id)}/structure`, brandId, teamId));
}

export function fetchDocumentEvidence(brandId: string, id: string, contentId: string, teamId?: string, expectedVersion?: number) {
  const path = documentPath(`/api/documents/${encodeURIComponent(id)}/contents/${encodeURIComponent(contentId)}`, brandId, teamId);
  return apiJson<DocumentEvidence>(`${path}${expectedVersion ? `&expected_version=${expectedVersion}` : ""}`);
}

export function fetchDocumentAsset(brandId: string, id: string, assetId: string, teamId?: string, expectedVersion?: number) {
  const path = documentPath(`/api/documents/${encodeURIComponent(id)}/assets/${encodeURIComponent(assetId)}`, brandId, teamId);
  return apiBlob(`${path}${expectedVersion ? `&expected_version=${expectedVersion}` : ""}`);
}

export function uploadBrandDocument(brandId: string, file: File, teamId?: string) {
  const form = new FormData();
  form.append("file", file);
  return apiForm<BrandDocument>(documentPath("/api/documents", brandId, teamId), form);
}

export function deleteBrandDocument(brandId: string, id: string, expectedVersion: number, teamId?: string) {
  return apiJson<{ id: string; deleted_chunks: number; version: number }>(
    `${documentPath(`/api/documents/${encodeURIComponent(id)}`, brandId, teamId)}&expected_version=${expectedVersion}`,
    { method: "DELETE" },
  );
}

export function queryBrandDocuments(brandId: string, question: string, teamId?: string) {
  const path = teamId ? `/api/query?team_id=${encodeURIComponent(teamId)}` : "/api/query";
  return apiJson<BrandQueryResult>(path, {
    method: "POST",
    body: JSON.stringify({ question, brand_id: brandId }),
  });
}

import { useEffect, useMemo, useState } from "react";
import { Link, useLocation, useParams, useSearchParams } from "react-router";
import { fetchBrandDocumentFile, fetchDocumentEvidence, fetchDocumentStructure, listBrandDocuments, type DocumentEvidence, type DocumentStructure } from "../../api/documents";
import { ApiError } from "../../api/http";
import { useAuthStore } from "../../store/authStore";
import { useLiveTeamStore } from "../../workbench/liveTeamStore";
import EvidenceContent from "./EvidenceContent";
import PdfViewer from "./PdfViewer";

type PreviewState = {
  filename?: string;
};

const ZOOM_STEPS = [0.75, 1, 1.25, 1.5, 2];

function isPdfFile(filename: string, mediaType: string) {
  return (
    filename.toLowerCase().endsWith(".pdf") ||
    mediaType.toLowerCase().includes("pdf")
  );
}

const AssetPreview = () => {
  const { documentId } = useParams<{ documentId: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const location = useLocation();
  const brandId = searchParams.get("brand");
  const hintedName = (location.state as PreviewState | null)?.filename;
  const userId = useAuthStore((state) => state.user?.id);
  const live = useLiveTeamStore();
  const refreshTeams = live.refreshTeams;
  const teamId = searchParams.get("team") || (live.userId === userId && live.status === "ready" ? live.membership?.team_id : null);
  const requestedPage = Number(searchParams.get("page"));
  const activePage = Number.isInteger(requestedPage) && requestedPage > 0 ? requestedPage : 1;
  const contentId = searchParams.get("content");
  const requestedVersion = Number(searchParams.get("version"));

  const [filename, setFilename] = useState(hintedName ?? "Document");
  const [mediaType, setMediaType] = useState("");
  const [buffer, setBuffer] = useState<ArrayBuffer | null>(null);
  const [text, setText] = useState<string | null>(null);
  const [pageCount, setPageCount] = useState(0);
  const [zoom, setZoom] = useState(1);
  const [fileScope, setFileScope] = useState("");
  const [fileError, setError] = useState<string | null>(null);
  const [savedStructure, setStructure] = useState<DocumentStructure | null>(null);
  const [structureError, setStructureError] = useState<string | null>(null);
  const [structureScope, setStructureScope] = useState("");
  const [evidence, setEvidence] = useState<DocumentEvidence | null>(null);
  const [evidenceError, setEvidenceError] = useState<string | null>(null);
  const [evidenceScope, setEvidenceScope] = useState("");
  const [reload, setReload] = useState(0);
  const [versionData, setVersionData] = useState<{ scope: string; version?: number; error?: string }>({ scope: "" });

  const scope = `${userId}:${teamId}:${brandId}:${documentId}:${reload}`;
  const documentVersion = versionData.scope === scope ? versionData.version : undefined;
  const fileKey = `${scope}:${documentVersion}`;
  const loading = fileScope !== fileKey;
  const error = !documentId || !brandId ? "Missing document or brand." : !teamId && live.status === "error" ? live.error : versionData.scope === scope && versionData.error ? versionData.error : fileScope === fileKey ? fileError : null;
  const structure = structureScope === scope ? savedStructure : null;
  const structureLoading = structureScope !== scope;
  const expectedVersion = requestedVersion > 0 ? requestedVersion : structure?.version;
  const evidenceKey = `${scope}:${contentId}:${expectedVersion}`;
  const evidenceLoading = evidenceScope !== evidenceKey;

  const pdf = isPdfFile(filename, mediaType);
  const assetsQuery = new URLSearchParams();
  if (brandId) assetsQuery.set("brand", brandId);
  if (teamId) assetsQuery.set("team", teamId);
  const assetsHref = assetsQuery.size ? `/assets?${assetsQuery}` : "/assets";
  const selectedContent = structure?.structured?.contents.find((item) => item.content_id === contentId);
  const selectedEvidence = evidenceScope === evidenceKey && evidence?.content_id === contentId && evidence.document_id === documentId ? evidence : null;
  const versionChanged = Boolean(structure && requestedVersion > 0 && requestedVersion !== structure.version);

  useEffect(() => {
    if (userId) void refreshTeams(userId);
  }, [refreshTeams, userId]);

  useEffect(() => {
    if (!documentId || !brandId || !teamId || !documentVersion) return;

    let cancelled = false;

    fetchBrandDocumentFile(brandId, documentId, teamId, documentVersion)
      .then(async ({ blob, filename: saved, mediaType: type }) => {
        const pdfBuffer = isPdfFile(saved || hintedName || "", type) ? await blob.arrayBuffer() : null;
        const plainText = pdfBuffer ? null : await blob.text();
        if (cancelled) return;
        setError(null);
        setFilename(saved || hintedName || "Document");
        setMediaType(type);
        setBuffer(pdfBuffer);
        setText(plainText);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(
            err instanceof ApiError && err.status === 409 ? "This document changed while its preview was loading. Reload the current document to inspect matching evidence." : err instanceof Error ? err.message : "Unable to open this file.",
          );
        }
      })
      .finally(() => {
        if (!cancelled) setFileScope(fileKey);
      });

    return () => {
      cancelled = true;
    };
  }, [brandId, documentId, documentVersion, fileKey, hintedName, teamId]);

  useEffect(() => {
    if (!documentId || !brandId || !teamId) return;
    let cancelled = false;
    void fetchDocumentStructure(brandId, documentId, teamId).then((result) => {
      if (!cancelled) { setStructure(result); setStructureError(null); setVersionData({ scope, version: result.version }); }
    }).catch(async (err) => {
      if (!cancelled) { setStructure(null); setStructureError(err instanceof Error ? err.message : "Unable to load document evidence."); }
      if (cancelled) return;
      // Historical files still have a versioned original, even when they predate
      // structured parsing. Resolve that version before requesting their bytes.
      if (err instanceof ApiError && err.status === 409) {
        try {
          const documents = await listBrandDocuments(brandId, teamId);
          const record = documents.find((item) => item.id === documentId);
          if (!cancelled) setVersionData(record ? { scope, version: record.version } : { scope, error: "This document is no longer available." });
        } catch (versionError) {
          if (!cancelled) setVersionData({ scope, error: versionError instanceof Error ? versionError.message : "Unable to verify the document version." });
        }
      } else {
        setVersionData({ scope, error: err instanceof Error ? err.message : "Unable to verify the document version." });
      }
    }).finally(() => { if (!cancelled) setStructureScope(scope); });
    return () => { cancelled = true; };
  }, [brandId, documentId, scope, teamId]);

  useEffect(() => {
    if (!documentId || !brandId || !teamId || !contentId || !expectedVersion) return;
    let cancelled = false;
    void fetchDocumentEvidence(brandId, documentId, contentId, teamId, expectedVersion).then((result) => {
      if (!cancelled) { setEvidence(result); setEvidenceError(null); }
    }).catch((err) => {
      if (!cancelled) { setEvidence(null); setEvidenceError(err instanceof Error ? err.message : "Unable to load this evidence item."); }
    }).finally(() => { if (!cancelled) setEvidenceScope(evidenceKey); });
    return () => { cancelled = true; };
  }, [brandId, contentId, documentId, evidenceKey, expectedVersion, teamId]);

  const selectContent = (id: string, page: number | null | undefined) => {
    const next = new URLSearchParams(searchParams);
    next.set("content", id);
    if (page) next.set("page", String(page));
    if (structure) next.set("version", String(structure.version));
    setSearchParams(next, { replace: true });
  };

  const selectPage = (page: number) => {
    const next = new URLSearchParams(searchParams);
    next.set("page", String(page));
    next.delete("content");
    setSearchParams(next, { replace: true });
  };

  const reloadCurrentDocument = () => {
    const next = new URLSearchParams(searchParams);
    next.delete("version");
    next.delete("content");
    setSearchParams(next, { replace: true });
    setReload((value) => value + 1);
  };

  const zoomLabel = useMemo(() => `${Math.round(zoom * 100)}%`, [zoom]);

  const shiftZoom = (delta: number) => {
    const current = Math.max(0, ZOOM_STEPS.indexOf(zoom));
    const next = ZOOM_STEPS[current + delta];
    if (next) setZoom(next);
  };

  return (
    <main className="gp-page gp-page--wide">
      <div className="gp-page__inner">
        <header className="gp-asset-preview__head">
          <div>
            <p className="gp-kicker">Library</p>
            <h1 className="gp-page__title gp-asset-preview__title">{loading ? hintedName || "Document" : filename}</h1>
            <p className="gp-page__lede">
              {loading
                ? "Loading preview…"
                : error
                  ? "This file could not be opened."
                  : pdf
                    ? pageCount
                      ? `${pageCount} page${pageCount === 1 ? "" : "s"} · in-app preview`
                      : "In-app PDF preview"
                    : "In-app text preview"}
            </p>
          </div>
          <div className="gp-asset-preview__actions">
            {pdf && buffer && !error && !loading ? (
              <label className="gp-evidence-page-picker">
                Page
                <select aria-label="PDF page" value={Math.min(activePage, pageCount || activePage)} onChange={(event) => selectPage(Number(event.target.value))}>
                  {Array.from({ length: pageCount || 1 }, (_, index) => <option key={index + 1} value={index + 1}>{index + 1}</option>)}
                </select>
              </label>
            ) : null}
            {pdf && buffer && !error && !loading ? (
              <div className="gp-asset-preview__zoom" role="group" aria-label="Zoom">
                <button
                  type="button"
                  className="gp-btn gp-btn--ghost gp-btn--sm"
                  disabled={zoom <= ZOOM_STEPS[0]}
                  onClick={() => shiftZoom(-1)}
                >
                  −
                </button>
                <span className="gp-asset-preview__zoom-label">{zoomLabel}</span>
                <button
                  type="button"
                  className="gp-btn gp-btn--ghost gp-btn--sm"
                  disabled={zoom >= ZOOM_STEPS[ZOOM_STEPS.length - 1]}
                  onClick={() => shiftZoom(1)}
                >
                  +
                </button>
              </div>
            ) : null}
            <Link to={assetsHref} className="gp-btn gp-btn--ghost gp-btn--sm">
              Back to Assets
            </Link>
          </div>
        </header>

        {versionChanged ? <div className="gp-evidence-notice" role="status"><p>This document has changed since the answer was generated. Showing the current document, version {structure?.version}; verify the evidence again.</p><button type="button" className="gp-btn gp-btn--ghost gp-btn--sm" onClick={reloadCurrentDocument}>Reload current document</button></div> : null}
        {error ? (
          <div className="gp-empty">
            <p className="gp-empty__title">Can’t preview this file</p>
            <p className="gp-empty__copy">{error}</p>
            <button type="button" className="gp-btn gp-btn--primary gp-btn--sm mt-5" onClick={reloadCurrentDocument}>Reload current document</button>
            <Link to={assetsHref} className="gp-btn gp-btn--primary gp-btn--sm mt-5">
              Back to Assets
            </Link>
          </div>
        ) : loading ? (
          <p className="gp-panel__meta">Loading preview…</p>
        ) : <div className="gp-evidence-layout">
          <div>{pdf && buffer ? (
            <PdfViewer data={buffer} zoom={zoom} onPageCount={setPageCount} activePage={activePage} highlights={versionChanged ? [] : selectedEvidence?.provenance ?? selectedContent?.provenance ?? []} />
          ) : <pre className="gp-asset-preview__text">{text}</pre>}</div>
          <aside className="gp-evidence-panel" aria-label="Document evidence">
            <div className="gp-evidence-panel__head"><h2>Document evidence</h2><p>Read extracted text, tables and images alongside the original.</p></div>
            {structureLoading ? <p className="gp-panel__meta">Loading document evidence…</p> : structureError ? <p className="gp-assets__error">{structureError}</p> : !structure?.structured ? <p className="gp-panel__meta">Structured evidence is unavailable for this document. Upload the PDF again to include OCR, tables and page references.</p> : <>
              <p className="gp-panel__meta">{structure.structured.page_count} pages · {structure.structured.contents.length} content items · version {structure.version}</p>
              <div className="gp-evidence-list" aria-label="Extracted content in reading order">
                {structure.structured.contents.map((item) => <button key={item.content_id} type="button" className={`gp-evidence-item${contentId === item.content_id ? " is-active" : ""}`} onClick={() => selectContent(item.content_id, item.page_no)} aria-pressed={contentId === item.content_id}>
                  <span className="gp-evidence-item__meta">{item.kind} · {item.page_no ? `Page ${item.page_no}` : "No page reference"}</span>
                  {item.section_path?.length ? <span className="gp-evidence-item__section">{item.section_path.join(" › ")}</span> : null}
                  <span>{(item.text || item.description || item.markdown || "Image").slice(0, 150)}</span>
                </button>)}
              </div>
              {contentId ? <section className="gp-evidence-selected" aria-label="Original evidence">
                <h3>Original evidence{selectedContent?.page_no ? ` · Page ${selectedContent.page_no}` : ""}</h3>
                {evidenceLoading ? <p className="gp-panel__meta">Loading original evidence…</p> : evidenceError ? <p className="gp-assets__error">{evidenceError}</p> : selectedEvidence && brandId && documentId && teamId ? <>
                  <p className="gp-evidence-selected__id">Content ID: {selectedEvidence.content_id} · version {selectedEvidence.document_version}</p>
                  <EvidenceContent content={selectedEvidence} brandId={brandId} documentId={documentId} teamId={teamId} version={selectedEvidence.document_version} />
                </> : null}
              </section> : <p className="gp-panel__meta">Select an item to inspect its original evidence and locate it on the PDF.</p>}
            </>}
          </aside>
        </div>}
      </div>
    </main>
  );
};

export default AssetPreview;

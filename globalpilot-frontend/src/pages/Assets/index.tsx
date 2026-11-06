import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";
import { ApiError } from "../../api/http";
import { useAuthStore } from "../../store/authStore";
import { useLiveTeamStore } from "../../workbench/liveTeamStore";
import {
  deleteBrandDocument,
  listBrandDocuments,
  listHardwareBrands,
  queryBrandDocuments,
  uploadBrandDocument,
  type BrandDocument,
  type HardwareBrand,
  type BrandQueryResult,
} from "../../api/documents";

function fileKind(name: string) {
  return name.toLowerCase().endsWith(".pdf") ? "PDF" : "TXT";
}

function formatWhen(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

const Assets = () => {
  const fileRef = useRef<HTMLInputElement>(null);
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const [brandRecords, setBrands] = useState<HardwareBrand[]>([]);
  const [brandsTeamId, setBrandsTeamId] = useState<string | null>(null);
  const [docRecords, setDocs] = useState<BrandDocument[]>([]);
  const [documentsScope, setDocumentsScope] = useState("");
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [question, setQuestion] = useState("");
  const [querying, setQuerying] = useState(false);
  const [queryError, setQueryError] = useState<string | null>(null);
  const [queryResult, setQueryResult] = useState<{ scope: string; result: BrandQueryResult } | null>(null);
  const userId = useAuthStore((state) => state.user?.id);
  const live = useLiveTeamStore();
  const refreshTeams = live.refreshTeams;
  const selectTeam = live.selectTeam;
  const requestedTeamId = searchParams.get("team");
  const teamId = live.userId === userId && live.status === "ready" && (!requestedTeamId || requestedTeamId === live.membership?.team_id) ? live.membership?.team_id ?? null : null;
  const unavailableTeam = live.userId === userId && live.status === "ready" && requestedTeamId && !live.teams.some((team) => team.team_id === requestedTeamId);
  const brands = useMemo(() => brandsTeamId === teamId ? brandRecords : [], [brandRecords, brandsTeamId, teamId]);

  const brandFromUrl = searchParams.get("brand");
  const brandId =
    brandFromUrl && brands.some((brand) => brand.id === brandFromUrl)
      ? brandFromUrl
      : null;
  const activeBrand = brands.find((brand) => brand.id === brandId) ?? null;
  const scope = `${userId ?? ""}:${teamId ?? ""}:${brandId ?? ""}`;
  const currentScope = useRef(scope);
  const docs = documentsScope === scope ? docRecords : [];
  const docsLoading = documentsScope !== scope;
  const result = queryResult?.scope === scope ? queryResult.result : null;

  useEffect(() => { currentScope.current = scope; }, [scope]);

  useEffect(() => {
    if (userId) void refreshTeams(userId);
  }, [refreshTeams, userId]);

  useEffect(() => {
    if (live.userId === userId && live.status === "ready" && requestedTeamId && requestedTeamId !== live.membership?.team_id && live.teams.some((team) => team.team_id === requestedTeamId)) void selectTeam(requestedTeamId);
  }, [live.membership?.team_id, live.status, live.teams, live.userId, requestedTeamId, selectTeam, userId]);

  useEffect(() => {
    if (!teamId) return;
    let cancelled = false;
    listHardwareBrands(teamId)
      .then((records) => {
        if (cancelled) return;
        setBrands(records);
        setBrandsTeamId(teamId);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(
            err instanceof Error ? err.message : "Unable to load brands.",
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, [teamId]);

  useEffect(() => {
    if (!brands.length) return;
    const preferred = searchParams.get("brand");
    const valid = Boolean(
      preferred && brands.some((brand) => brand.id === preferred),
    );
    if (!valid) {
      const next = brands[0]?.id;
      if (next) {
        const params = new URLSearchParams(searchParams);
        params.set("brand", next);
        setSearchParams(params, { replace: true });
      }
    }
  }, [brands, searchParams, setSearchParams]);

  useEffect(() => {
    if (!brandId || !teamId) return;
    let cancelled = false;
    listBrandDocuments(brandId, teamId)
      .then((records) => {
        if (!cancelled) setDocs(records);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(
            err instanceof Error ? err.message : "Unable to load documents.",
          );
          setDocs([]);
        }
      })
      .finally(() => {
        if (!cancelled) setDocumentsScope(scope);
      });
    return () => {
      cancelled = true;
    };
  }, [brandId, scope, teamId]);

  const selectBrand = (id: string) => {
    if (id === brandId) return;
    setError(null);
    const params = new URLSearchParams(searchParams);
    params.set("brand", id);
    setSearchParams(params, { replace: true });
  };

  const onOpenDoc = (doc: BrandDocument) => {
    if (!brandId || !teamId) return;
    navigate(`/assets/${doc.id}?${new URLSearchParams({ brand: brandId, team: teamId })}`, {
      state: { filename: doc.source },
    });
  };

  const onUpload = async (files: FileList | null) => {
    if (!files?.length || !brandId || !teamId) return;
    const operationScope = scope;
    setError(null);
    setUploading(true);
    try {
      for (const file of Array.from(files)) {
        const lower = file.name.toLowerCase();
        if (!lower.endsWith(".txt") && !lower.endsWith(".pdf")) {
          throw new Error("Only .txt and .pdf files are supported.");
        }
        await uploadBrandDocument(brandId, file, teamId);
      }
      const nextDocs = await listBrandDocuments(brandId, teamId);
      if (currentScope.current === operationScope) { setDocs(nextDocs); setDocumentsScope(operationScope); }
    } catch (err) {
      if (currentScope.current === operationScope) setError(err instanceof Error ? err.message : "Upload failed.");
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const onRemoveDoc = async (document: BrandDocument) => {
    if (!brandId || !teamId) return;
    const operationScope = scope;
    setError(null);
    try {
      await deleteBrandDocument(brandId, document.id, document.version, teamId);
      if (currentScope.current === operationScope) setDocs((prev) => prev.filter((doc) => doc.id !== document.id));
    } catch (err) {
      if (currentScope.current !== operationScope) return;
      setError(err instanceof Error ? err.message : "Delete failed.");
      if (err instanceof ApiError && (err.status === 409 || err.status === 404)) {
        try {
          const latest = await listBrandDocuments(brandId, teamId);
          if (currentScope.current === operationScope) setDocs(latest);
        } catch {
          // Keep the conflict visible; do not retry deletion with a newer version.
        }
      }
    }
  };

  const onQuery = async (event: React.SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!brandId || !teamId || !question.trim() || querying) return;
    const queryScope = scope;
    setQuerying(true);
    setQueryError(null);
    try {
      const answer = await queryBrandDocuments(brandId, question.trim(), teamId);
      if (currentScope.current === queryScope) setQueryResult({ scope: queryScope, result: answer });
    } catch (err) {
      if (currentScope.current === queryScope) setQueryError(err instanceof Error ? err.message : "Unable to search these documents.");
    } finally {
      setQuerying(false);
    }
  };

  return (
    <main className="gp-page gp-page--wide">
      <div className="gp-page__inner">
        <header className="gp-page__head">
          <div>
            <p className="gp-kicker">Library</p>
            <h1 className="gp-page__title">Assets</h1>
            <p className="gp-page__lede">
              Upload PDF or TXT brand documents. PDF processing includes OCR,
              tables, images and page references.
            </p>
          </div>
          <div className="gp-asset-preview__actions">
          <label className="gp-evidence-page-picker">Team
            <select aria-label="Assets team" value={live.currentTeamId ?? ""} disabled={live.status === "loading" || uploading || querying} onChange={(event) => { setError(null); setSearchParams({ team: event.target.value }, { replace: true }); void selectTeam(event.target.value); }}>
              {live.userId === userId ? live.teams.map((team) => <option key={team.team_id} value={team.team_id}>{team.team_name}</option>) : null}
            </select>
          </label>
          <button
            type="button"
            className="gp-btn gp-btn--primary gp-btn--sm"
            disabled={uploading || !brandId}
            onClick={() => fileRef.current?.click()}
          >
            {uploading ? "Processing documents…" : "Upload txt / PDF"}
          </button>
          </div>
          <input
            ref={fileRef}
            type="file"
            accept=".txt,.pdf"
            multiple
            hidden
            onChange={(e) => void onUpload(e.target.files)}
          />
        </header>

        {error || live.error || unavailableTeam ? <p className="gp-assets__error" role="alert">{unavailableTeam ? "This team is unavailable. Select one of your current teams." : error || live.error}</p> : null}

        <div className="gp-workspace">
          <aside className="gp-rail" aria-label="Brands">
            <p className="gp-rail__label">Brands</p>
            <div className="gp-rail__list">
              {brands.map((brand) => (
                <button
                  key={brand.id}
                  type="button"
                  className={`gp-rail__item${brandId === brand.id ? " is-active" : ""}`}
                  onClick={() => selectBrand(brand.id)}
                >
                  <span className="gp-rail__item-title">{brand.name}</span>
                  <span className="gp-rail__item-meta">
                    {brandId === brand.id
                      ? `${docs.length} files`
                      : "Hardware line"}
                  </span>
                </button>
              ))}
            </div>
          </aside>

          <section>
            {docsLoading || !brandId ? (
              <p className="gp-panel__meta">Loading documents…</p>
            ) : docs.length === 0 ? (
              <div className="gp-empty">
                <p className="gp-empty__title">
                  No {activeBrand?.name.toLowerCase() ?? "brand"} documents yet
                </p>
                <p className="gp-empty__copy">
                  Upload a TXT or PDF into this brand. Files stay isolated within
                  this team and brand.
                </p>
                <button
                  type="button"
                  className="gp-btn gp-btn--primary gp-btn--sm mt-5"
                  disabled={uploading}
                  onClick={() => fileRef.current?.click()}
                >
                  Upload txt / PDF
                </button>
              </div>
            ) : (
              <div className="gp-assets__grid">
                {docs.map((doc) => (
                  <article key={doc.id} className="gp-asset-card">
                    <button
                      type="button"
                      className="gp-asset-card__open"
                      onClick={() => onOpenDoc(doc)}
                    >
                      <span className="gp-asset-card__kind">
                        {fileKind(doc.source)}
                      </span>
                      <span className="gp-asset-card__title">{doc.source}</span>
                      <span className="gp-asset-card__updated">
                        Updated {formatWhen(doc.updated_at)}
                      </span>
                    </button>
                    <button
                      type="button"
                      className="gp-asset-card__remove"
                      aria-label={`Delete ${doc.source}`}
                      onClick={() => void onRemoveDoc(doc)}
                    >
                      <svg
                        width="16"
                        height="16"
                        viewBox="0 0 16 16"
                        fill="none"
                        aria-hidden
                      >
                        <path
                          d="M3.5 4.5h9M6.5 4.5V3h3v1.5M4.5 4.5l.6 8h5.8l.6-8"
                          stroke="currentColor"
                          strokeWidth="1.4"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        />
                      </svg>
                    </button>
                  </article>
                ))}
              </div>
            )}
          {brandId && teamId ? <section className="gp-knowledge-query" aria-label="Ask brand documents">
            <h2>Ask brand documents</h2>
            <p className="gp-panel__meta">Search this brand’s documents and inspect the original page behind each answer.</p>
            <form onSubmit={(event) => void onQuery(event)}>
              <label htmlFor="gp-knowledge-question">Your question</label>
              <textarea id="gp-knowledge-question" value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="What do the product documents say?" rows={3} required disabled={querying} />
              <button className="gp-btn gp-btn--primary gp-btn--sm" type="submit" disabled={querying || !question.trim() || !docs.length}>{querying ? "Searching documents…" : "Ask documents"}</button>
            </form>
            {queryError ? <p className="gp-assets__error" role="alert">{queryError}</p> : null}
            {result ? <div className="gp-knowledge-answer" aria-live="polite">
              <h3>Answer</h3><p>{result.answer}</p>
              {result.sources.length ? <><h3>Sources</h3><ol className="gp-knowledge-sources">
                {result.sources.map((source, index) => {
                  const evidence = source.evidence?.[0];
                  const page = source.page_no ?? source.page_numbers?.[0] ?? evidence?.page_no;
                  const content = source.content_id ?? source.content_ids?.[0] ?? evidence?.content_id;
                  const params = new URLSearchParams({ brand: brandId, team: teamId });
                  if (page) params.set("page", String(page));
                  if (content) params.set("content", content);
                  if (source.document_version) params.set("version", String(source.document_version));
                  return <li key={`${source.document_id}:${source.chunk_index}:${index}`}>
                    <Link to={`/assets/${encodeURIComponent(source.document_id)}?${params}`} state={{ filename: source.source }}>{source.source}{page ? ` · Page ${page}` : " · Open source"}</Link>
                    <p>{source.content}</p>
                    {source.evidence && source.evidence.length > 1 ? <div className="gp-knowledge-sources__additional">{source.evidence.slice(1).map((item) => {
                      const evidenceParams = new URLSearchParams(params);
                      evidenceParams.set("content", item.content_id);
                      if (item.page_no) evidenceParams.set("page", String(item.page_no));
                      return <Link key={item.content_id} to={`/assets/${encodeURIComponent(source.document_id)}?${evidenceParams}`}>{item.page_no ? `Page ${item.page_no}` : "Evidence"} · {item.kind}</Link>;
                    })}</div> : null}
                  </li>;
                })}
              </ol></> : <p className="gp-panel__meta">No supporting document evidence was found.</p>}
            </div> : null}
          </section> : null}
          </section>
        </div>
      </div>
    </main>
  );
};

export default Assets;

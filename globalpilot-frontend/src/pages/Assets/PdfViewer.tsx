import { useCallback, useEffect, useRef, useState } from "react";
import { getDocument, GlobalWorkerOptions } from "pdfjs-dist";
import type { PDFDocumentProxy, PDFPageProxy, RenderTask } from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import type { ContentProvenance } from "../../api/documents";

GlobalWorkerOptions.workerSrc = workerUrl;
const NO_HIGHLIGHTS: ContentProvenance[] = [];

type PdfViewerProps = {
  data: ArrayBuffer;
  zoom: number;
  onPageCount?: (count: number) => void;
  activePage?: number;
  highlights?: ContentProvenance[];
};

function highlightStyle(item: ContentProvenance) {
  const { bbox, page_width: width, page_height: height, coord_origin: origin } = item;
  if (!bbox || !width || !height || width <= 0 || height <= 0 || ![bbox.l, bbox.t, bbox.r, bbox.b, width, height].every(Number.isFinite)) return null;
  if (origin !== "TOPLEFT" && origin !== "BOTTOMLEFT") return null;
  const left = Math.max(0, Math.min(width, Math.min(bbox.l, bbox.r)));
  const right = Math.max(left, Math.min(width, Math.max(bbox.l, bbox.r)));
  const top = Math.max(0, Math.min(height, origin === "BOTTOMLEFT" ? height - Math.max(bbox.t, bbox.b) : Math.min(bbox.t, bbox.b)));
  const bottom = Math.max(top, Math.min(height, origin === "BOTTOMLEFT" ? height - Math.min(bbox.t, bbox.b) : Math.max(bbox.t, bbox.b)));
  if (right <= left || bottom <= top) return null;
  return { left: `${left / width * 100}%`, top: `${top / height * 100}%`, width: `${(right - left) / width * 100}%`, height: `${(bottom - top) / height * 100}%` };
}

function PdfPage({ pdf, number, width, zoom, active, highlights, onLayout }: { pdf: PDFDocumentProxy; number: number; width: number; zoom: number; active: boolean; highlights: ContentProvenance[]; onLayout: () => void }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [page, setPage] = useState<PDFPageProxy | null>(null);
  const [error, setError] = useState<string | null>(null);
  const unscaled = page?.getViewport({ scale: 1 });
  const viewport = page && unscaled ? page.getViewport({ scale: width / unscaled.width * zoom }) : null;
  const renderedWidth = Math.max(1, Math.floor(viewport?.width ?? width * zoom));
  const renderedHeight = Math.max(1, Math.floor(viewport?.height ?? width * zoom * 1.294));

  useEffect(() => {
    let cancelled = false;
    void pdf.getPage(number).then((value) => {
      if (!cancelled) { setPage(value); setError(null); }
    }).catch((failure) => {
      if (!cancelled) setError(failure instanceof Error ? failure.message : "Unable to load this PDF page.");
    });
    return () => { cancelled = true; };
  }, [number, pdf]);

  useEffect(() => {
    if (!page || width < 160) return;
    const canvas = canvasRef.current;
    if (!canvas) return;
    let cancelled = false;
    let task: RenderTask | undefined;
    const render = async () => {
      // Let cancellation release a canvas before starting its next render.
      await Promise.resolve();
      if (cancelled) return;
      const natural = page.getViewport({ scale: 1 });
      const view = page.getViewport({ scale: width / natural.width * zoom });
      const outputScale = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.floor(view.width * outputScale);
      canvas.height = Math.floor(view.height * outputScale);
      try {
        task = page.render({ canvas, viewport: view, transform: outputScale !== 1 ? [outputScale, 0, 0, outputScale, 0, 0] : undefined });
        await task.promise;
        if (!cancelled) setError(null);
      } catch (failure) {
        if (!cancelled) setError(failure instanceof Error ? failure.message : "Unable to render this PDF page.");
      }
    };
    void render();
    return () => { cancelled = true; task?.cancel(); };
  }, [page, width, zoom]);

  useEffect(() => {
    if (page) onLayout();
  }, [onLayout, page]);

  return <section className={`gp-pdf-sheet${active ? " is-active" : ""}`} data-page={number} aria-label={`Page ${number}`}>
    <p className="gp-pdf-sheet__label">Page {number}</p>
    <div className="gp-pdf-sheet__canvas" style={{ width: renderedWidth, height: renderedHeight }}>
      <canvas ref={canvasRef} className="gp-asset-preview__page" style={{ width: renderedWidth, height: renderedHeight }} />
      {highlights.filter((item) => item.page_no === number).map((item, index) => {
        const style = highlightStyle(item);
        return style ? <span key={index} className="gp-pdf-highlight" style={style} aria-hidden="true" /> : null;
      })}
    </div>
    {error ? <p className="gp-assets__error">{error}</p> : null}
  </section>;
}

const PdfViewer = ({ data, zoom, onPageCount, activePage = 1, highlights = NO_HIGHLIGHTS }: PdfViewerProps) => {
  const measureRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  const [loaded, setLoaded] = useState<{ data: ArrayBuffer; pdf?: PDFDocumentProxy; error?: string } | null>(null);
  const [layoutVersion, setLayoutVersion] = useState(0);
  const onPageLayout = useCallback(() => setLayoutVersion((version) => version + 1), [setLayoutVersion]);
  const pdf = loaded?.data === data ? loaded.pdf : undefined;
  const error = loaded?.data === data ? loaded.error : undefined;

  useEffect(() => {
    const element = measureRef.current;
    if (!element) return;
    const observer = new ResizeObserver((entries) => {
      const next = Math.floor(entries[0]?.contentRect.width ?? 0);
      setWidth((previous) => Math.abs(previous - next) < 2 ? previous : next);
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    let cancelled = false;
    // Resizing or choosing evidence only redraws pages; it never destroys the
    // document worker or empties the entire page container.
    const task = getDocument({ data: new Uint8Array(data.slice(0)) });
    void task.promise.then((document) => {
      if (!cancelled) setLoaded({ data, pdf: document });
    }).catch((failure) => {
      if (!cancelled) setLoaded({ data, error: failure instanceof Error ? failure.message : "Unable to open this PDF." });
    });
    return () => { cancelled = true; void task.destroy().catch(() => {}); };
  }, [data]);

  useEffect(() => { if (pdf) onPageCount?.(pdf.numPages); }, [onPageCount, pdf]);

  useEffect(() => {
    if (!pdf) return;
    const frame = requestAnimationFrame(() => {
      const stage = measureRef.current;
      if (!stage) return;
      const sheet = stage.querySelector<HTMLElement>(`.gp-pdf-sheet[data-page="${activePage}"]`);
      if (!sheet) return;
      const highlight = sheet.querySelector<HTMLElement>(".gp-pdf-highlight");
      const target = highlight ?? sheet;
      const bounds = stage.getBoundingClientRect();
      const targetBounds = target.getBoundingClientRect();
      // Scroll only this viewport: scrollIntoView also moves the page/body and
      // can hide the document toolbar underneath the fixed application header.
      stage.scrollTo({
        top: Math.max(0, stage.scrollTop + targetBounds.top - bounds.top - stage.clientTop - 16),
        left: highlight ? Math.max(0, stage.scrollLeft + targetBounds.left - bounds.left - stage.clientLeft - 16) : 0,
        behavior: "smooth",
      });
    });
    return () => cancelAnimationFrame(frame);
  }, [activePage, highlights, layoutVersion, pdf, width, zoom]);

  return <div ref={measureRef} className="gp-asset-preview__stage">
    {error ? <p className="gp-assets__error">{error}</p> : !pdf || width < 160 ? <p className="gp-panel__meta">Rendering PDF pages…</p> : <div className="gp-asset-preview__pages">
      {Array.from({ length: pdf.numPages }, (_, index) => <PdfPage key={index + 1} pdf={pdf} number={index + 1} width={width} zoom={zoom} active={activePage === index + 1} highlights={highlights} onLayout={onPageLayout} />)}
    </div>}
  </div>;
};

export default PdfViewer;

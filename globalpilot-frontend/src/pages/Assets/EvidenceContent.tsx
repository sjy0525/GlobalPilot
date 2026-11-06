import { useEffect, useState } from "react";
import { fetchDocumentAsset, type DocumentContent } from "../../api/documents";

type TableCell = {
  text: string;
  start_row_offset_idx: number;
  end_row_offset_idx: number;
  start_col_offset_idx: number;
  end_col_offset_idx: number;
  column_header?: boolean;
  row_header?: boolean;
};

function tableCells(table: Record<string, unknown> | undefined): TableCell[] {
  const data = table?.data && typeof table.data === "object" ? table.data as Record<string, unknown> : table;
  if (!Array.isArray(data?.table_cells)) return [];
  return data.table_cells.filter((cell): cell is TableCell => {
    if (!cell || typeof cell !== "object") return false;
    const item = cell as Partial<TableCell>;
    return typeof item.text === "string" && [item.start_row_offset_idx, item.end_row_offset_idx, item.start_col_offset_idx, item.end_col_offset_idx].every((value) => typeof value === "number" && Number.isInteger(value) && value >= 0);
  });
}

function EvidenceTable({ content }: { content: DocumentContent }) {
  const cells = tableCells(content.table);
  if (cells.length) {
    const rows = [...new Set(cells.map((cell) => cell.start_row_offset_idx))].sort((a, b) => a - b);
    return <div className="gp-evidence-table"><table><tbody>{rows.map((row) => <tr key={row}>
      {cells.filter((cell) => cell.start_row_offset_idx === row).sort((a, b) => a.start_col_offset_idx - b.start_col_offset_idx).map((cell, index) => {
        const Tag = cell.column_header || cell.row_header ? "th" : "td";
        return <Tag key={index} rowSpan={Math.max(1, cell.end_row_offset_idx - cell.start_row_offset_idx)} colSpan={Math.max(1, cell.end_col_offset_idx - cell.start_col_offset_idx)}>{cell.text}</Tag>;
      })}
    </tr>)}</tbody></table></div>;
  }
  const markdown = content.markdown || content.text;
  const lines = markdown.split("\n").filter((line) => /^\s*\|/.test(line));
  const rows = lines.map((line) => line.trim().replace(/^\|/, "").replace(/\|$/, "").split(/(?<!\\)\|/).map((cell) => cell.trim().replace(/\\\|/g, "|"))).filter((row) => !row.every((cell) => /^:?-{2,}:?$/.test(cell)));
  if (!rows.length) return <p className="gp-evidence-content__text">{markdown}</p>;
  return <div className="gp-evidence-table"><table><tbody>{rows.map((row, index) => <tr key={index}>{row.map((cell, column) => index === 0 ? <th key={column}>{cell}</th> : <td key={column}>{cell}</td>)}</tr>)}</tbody></table></div>;
}

function EvidenceImage({ brandId, documentId, teamId, assetId, description, version }: { brandId: string; documentId: string; teamId: string; assetId: string; description: string; version: number }) {
  const scope = `${teamId}:${brandId}:${documentId}:${version}:${assetId}`;
  const [image, setImage] = useState<{ scope: string; url?: string; error?: string }>({ scope: "" });
  useEffect(() => {
    let cancelled = false;
    let url: string | undefined;
    void fetchDocumentAsset(brandId, documentId, assetId, teamId, version).then(({ blob }) => {
      if (cancelled) return;
      url = URL.createObjectURL(blob);
      setImage({ scope, url });
    }).catch((error) => {
      if (!cancelled) setImage({ scope, error: error instanceof Error ? error.message : "Unable to load image." });
    });
    return () => { cancelled = true; if (url) URL.revokeObjectURL(url); };
  }, [assetId, brandId, documentId, scope, teamId, version]);
  return image.scope !== scope ? <p className="gp-panel__meta">Loading image…</p> : image.error ? <p className="gp-assets__error">{image.error}</p> : <img className="gp-evidence-content__image" src={image.url} alt={description || "Extracted document image"} />;
}

function containsExactBlock(container: string, block: string) {
  const text = container.trim();
  const value = block.trim();
  if (!value) return false;
  const labels = ["图注：", "Caption:", "图片文字（OCR）：", "Image text (OCR):", "OCR:", "图片描述（模型生成）：", "图片描述：", "Image description:"];
  const candidates = [value, ...labels.flatMap((label) => [`${label}${value}`, `${label} ${value}`])];
  // Match a whole original block at line boundaries. Similar sentences or
  // shorter phrases inside another sentence remain distinct evidence.
  return candidates.some((candidate) => text === candidate || text.startsWith(`${candidate}\n`) || text.endsWith(`\n${candidate}`) || text.includes(`\n${candidate}\n`));
}

export default function EvidenceContent({ content, brandId, documentId, teamId, version }: { content: DocumentContent; brandId: string; documentId: string; teamId: string; version: number }) {
  const description = content.description?.trim() || "";
  const originalText = (content.text || content.markdown || "").trim();
  const primaryText = originalText === description ? "" : originalText;
  const descriptionIncluded = description && containsExactBlock(primaryText, description);
  const ocrIncluded = content.ocr_text && (containsExactBlock(primaryText, content.ocr_text) || containsExactBlock(description, content.ocr_text));
  const captionIncluded = content.caption && (containsExactBlock(primaryText, content.caption) || containsExactBlock(description, content.caption));
  return <div className="gp-evidence-content">
    {content.kind === "table" ? <>
      {content.caption ? <p className="gp-evidence-content__description">{content.caption}</p> : null}
      {content.context_text ? <p className="gp-evidence-content__description">{content.context_text}</p> : null}
      <EvidenceTable content={content} />
    </> : <>
      {content.kind === "picture" && content.asset_id ? <EvidenceImage brandId={brandId} documentId={documentId} teamId={teamId} assetId={content.asset_id} description={content.description || content.text} version={version} /> : null}
      {content.caption && !captionIncluded ? <p className="gp-evidence-content__description">Caption: {content.caption}</p> : null}
      {descriptionIncluded && content.description_is_generated ? <p className="gp-panel__meta">Includes an AI-generated image description{content.description_source ? ` · ${content.description_source}` : ""}.</p> : null}
      {primaryText ? <p className="gp-evidence-content__text">{content.kind === "picture" && primaryText === content.ocr_text?.trim() ? <strong>Image text (OCR): </strong> : null}{primaryText}</p> : null}
      {content.ocr_text && !ocrIncluded ? <p className="gp-evidence-content__description">Image text (OCR): {content.ocr_text}</p> : null}
      {description && !descriptionIncluded ? <p className="gp-evidence-content__description"><strong>{content.description_is_generated ? "AI-generated image description" : "Image description"}{content.description_source ? ` · ${content.description_source}` : ""}:</strong> {description}</p> : null}
      {!primaryText && !description && !content.ocr_text ? <p className="gp-panel__meta">No text available for this item.</p> : null}
    </>}
  </div>;
}

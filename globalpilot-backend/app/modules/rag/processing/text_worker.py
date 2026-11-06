"""Pure character splitting: strings/configuration in, string chunks out."""

import re


def split_text_parts(text: str, chunk_size: int, overlap: int) -> list[str]:
    # Reuse the same library algorithm as the synchronous application path.
    # No settings, database, embedding/LLM clients or Documents are imported here.
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    overflow = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=overlap,
        separators=["\n", "。", "！", "？", "；", ".", "!", "?", " ", ""],
    )
    paragraphs = [paragraph.strip() for paragraph in re.split(r"\n\s*\n", text) if paragraph.strip()]
    if not paragraphs:
        stripped = text.strip()
        return [stripped] if stripped else []
    chunks = []
    for paragraph in paragraphs:
        if len(paragraph) <= chunk_size:
            chunks.append(paragraph)
        else:
            chunks.extend(overflow.split_text(paragraph))
    return chunks

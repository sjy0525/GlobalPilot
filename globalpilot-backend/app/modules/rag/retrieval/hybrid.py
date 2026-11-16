from dataclasses import dataclass
from math import isfinite

from langchain_core.documents import Document


@dataclass
class ChannelScores:
    vector_rank: int | None = None
    vector_distance: float | None = None
    keyword_rank: int | None = None
    keyword_score: float | None = None

    def rrf(self, constant: int) -> float:
        return sum(1 / (constant + rank) for rank in (self.vector_rank, self.keyword_rank) if rank is not None)

    def metadata(self, rrf_score: float) -> dict:
        channels = []
        if self.vector_rank is not None:
            channels.append("vector")
        if self.keyword_rank is not None:
            channels.append("keyword")
        return {
            "vector_rank": self.vector_rank,
            "vector_distance": self.vector_distance,
            "keyword_rank": self.keyword_rank,
            "keyword_score": self.keyword_score,
            "rrf_score": rrf_score,
            "channels": channels,
        }


def merge_hits(vector_hits, keyword_hits, *, max_distance: float) -> list[tuple[Document, ChannelScores]]:
    merged: dict[tuple, tuple[Document, ChannelScores]] = {}
    for is_vector, hits in ((True, vector_hits), (False, keyword_hits)):
        for rank, (document, score) in enumerate(hits, 1):
            if not isfinite(score) or (is_vector and score > max_distance):
                continue
            metadata = document.metadata
            document_id = metadata.get("document_id")
            version = metadata.get("document_version")
            index = metadata.get("chunk_index")
            if not isinstance(document_id, str) or (version is not None and type(version) is not int):
                continue
            # 旧片段缺少序号时，按同一文档内的正文合并。
            if index is None:
                index = document.page_content
            if type(index) not in (int, str):
                continue
            key = (document_id, version, index)
            if key not in merged:
                merged[key] = (document, ChannelScores())
            scores = merged[key][1]
            if is_vector and scores.vector_rank is None:
                scores.vector_rank, scores.vector_distance = rank, score
            elif not is_vector and scores.keyword_rank is None:
                scores.keyword_rank, scores.keyword_score = rank, score
    return list(merged.values())


def rank_by_rrf(hits, *, constant: int, limit: int) -> list[tuple[Document, float | None]]:
    ranked = sorted(
        ((scores.rrf(constant), document, scores) for document, scores in hits if document.page_content.strip()),
        key=lambda hit: hit[0],
        reverse=True,
    )
    return [
        (Document(page_content=document.page_content,
                  metadata={**document.metadata, "retrieval": scores.metadata(rrf_score)}), scores.vector_distance)
        for rrf_score, document, scores in ranked[:limit]
    ]

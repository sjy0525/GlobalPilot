from langchain_openai import OpenAIEmbeddings
from app.core.config import settings

embeddings = OpenAIEmbeddings(
    model=settings.dashscope_embedding_model,
    api_key=settings.dashscope_api_key,
    base_url=settings.dashscope_base_url,
    dimensions=settings.dashscope_embedding_dimensions,
    chunk_size=10,
    check_embedding_ctx_length=False,
)
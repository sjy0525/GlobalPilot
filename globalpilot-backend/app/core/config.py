from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    #数据库
    database_url: str

    #阿里云百链
    dashscope_api_key:str
    dashscope_base_url:str
    dashscope_embedding_model:str="text-embedding-v4"
    dashscope_embedding_dimensions:int=1024
    dashscope_rerank_api_key:str
    dashscope_rerank_model:str="qwen3-rerank"
    # qwen3-rerank 走 Cohere 兼容接口，不是 embedding 用的 compatible-mode
    dashscope_rerank_base_url:str="https://dashscope.aliyuncs.com/compatible-api/v1"

    #deepseek
    deepseek_api_key:str
    deepseek_base_url:str = "https://api.deepseek.com/v1"
    deepseek_model:str="deepseek-v4-flash"

    # 百度搜索
    baidu_search_api_key: SecretStr = SecretStr("")
    baidu_search_url: str = "https://qianfan.baidubce.com/v2/ai_search/web_search"

    # rag
    rag_chunk_size: int = 500
    rag_chunk_overlap: int = 80
    # 过滤空块 / 弱相关 / 重复后，交给重排序的候选数
    rag_recall_k: int = 10
    # 重排序后进入上下文的片段数
    rag_search_k: int = 3
    # 向量库先多取一些，给过滤留余量
    rag_fetch_k: int = 20
    # PGVector cosine_distance：0 最相近，超过该值视为弱相关，不进入候选
    rag_max_distance: float = 0.64

    #jwt
    jwt_secret_key: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 14
    # Local HTTP development uses False; HTTPS deployments must set True.
    refresh_cookie_secure: bool = False
    auth_allowed_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    #google三方登录
    google_client_id: str = (
        "884569604770-nnsae7rk66rttqutmebfdq0o534sml4l.apps.googleusercontent.com"
    )
    model_config = SettingsConfigDict(env_file='.env', extra="ignore")

settings = Settings()

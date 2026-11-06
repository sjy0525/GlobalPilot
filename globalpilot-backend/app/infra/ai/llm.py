from langchain_deepseek import ChatDeepSeek
from app.core.config import settings

llm=ChatDeepSeek(
    model=settings.deepseek_model,
    api_key=settings.deepseek_api_key,
    base_url=settings.deepseek_base_url,
    temperature=0,
    max_tokens=1024,
    extra_body={
        "thinking":{
            "type":"disabled",
        }
    }
)
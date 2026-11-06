import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.infra.ai.llm import llm


response = llm.invoke(
    "请只回复：DeepSeek连接成功"
)


print(response.content)
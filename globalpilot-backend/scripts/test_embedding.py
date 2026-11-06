import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.infra.ai.embeddings import embeddings

text = "Hello, world!"
vector = embeddings.embed_query(text)
print("前十个数字是：")
print(vector[:10])

print("向量维度是：")
print(len(vector))

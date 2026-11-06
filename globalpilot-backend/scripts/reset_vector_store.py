import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.modules.rag.retrieval.vector_store import vector_store


vector_store.delete_collection()

vector_store.create_collection()


print("brand_knowledge 已清空并重新创建")
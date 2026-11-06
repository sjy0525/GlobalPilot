"""Manually search one explicit actor/team/category; uses configured providers."""

import argparse
import os
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("brand_id", type=UUID)
    parser.add_argument("--team-id", type=UUID, required=True)
    parser.add_argument("--actor-user-id", type=UUID, required=True)
    parser.add_argument("--database-url", default=os.environ.get("RAG_SEARCH_DATABASE_URL"))
    parser.add_argument("--question", default="我们的品牌语气是什么？")
    args = parser.parse_args()
    if not args.database_url:
        parser.error("Pass --database-url or RAG_SEARCH_DATABASE_URL explicitly")

    # Select the target before importing any lazy application engine/provider.
    os.environ["DATABASE_URL"] = args.database_url
    from app.modules.rag.service import search_documents

    docs = search_documents(
        args.question, args.brand_id,
        team_id=args.team_id, actor_user_id=args.actor_user_id,
    )
    if not docs:
        print("没有通过相似度过滤的片段")
    for doc, distance in docs:
        print(f"distance={distance:.4f}")
        print(doc.page_content)
        print(doc.metadata)


if __name__ == "__main__":
    main()

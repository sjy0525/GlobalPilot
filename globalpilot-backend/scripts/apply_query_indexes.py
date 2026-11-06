"""Install the EXPLAIN-verified indexes on an explicitly selected database.

Run after application schema migrations. Uses concurrent index builds and
never loads .env or starts the application/model providers.
"""
import argparse
import json
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.environ.get("QUERY_INDEX_DATABASE_URL"))
    args = parser.parse_args()
    if not args.database_url:
        parser.error("Set QUERY_INDEX_DATABASE_URL or supply --database-url explicitly")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.db.query_indexes import ensure_query_indexes
    try:
        result = ensure_query_indexes(args.database_url)
    except Exception as exc:
        # Driver errors may contain connection credentials or user data.
        print(f"Index installation did not complete ({type(exc).__name__}); inspect schema and index validity.", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

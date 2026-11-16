"""Populate lexical metadata on existing brand knowledge chunks.

Run after the application schema upgrade using an explicit PostgreSQL URL.
Completed batches stay committed, so rerunning resumes the remaining work.
"""
import argparse
import json
import os
from pathlib import Path
import sys

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from sqlalchemy.engine import make_url


_READ_BATCH = """
    SELECT e.id, e.collection_id, e.document, e.cmetadata,
           d.id AS document_id, d.version AS document_version
    FROM langchain_pg_embedding AS e
    JOIN langchain_pg_collection AS c ON c.uuid = e.collection_id
    JOIN knowledge_documents AS d ON d.id = CASE
        WHEN pg_input_is_valid(e.cmetadata ->> 'document_id', 'uuid')
        THEN (e.cmetadata ->> 'document_id')::uuid
    END
    JOIN brands AS b ON b.id = d.brand_id AND b.team_id = d.team_id
    WHERE c.name = 'brand_knowledge' AND e.id > %(after)s
      AND d.team_id::text = e.cmetadata ->> 'team_id'
      AND d.brand_id::text = e.cmetadata ->> 'brand_id'
      AND (e.cmetadata -> 'document_version' = to_jsonb(d.version)
           OR (e.cmetadata ->> 'document_version' IS NULL
               AND d.version = 1
               AND (d.structured IS NULL OR d.structured = 'null'::jsonb)))
      AND (e.cmetadata -> 'tokenizer_version' IS DISTINCT FROM to_jsonb(%(version)s::integer)
           OR jsonb_typeof(e.cmetadata -> 'lexical_text') IS DISTINCT FROM 'string')
    ORDER BY e.id
    LIMIT %(limit)s
"""


_UPDATE_CHUNK = """
    WITH current_chunk AS (
        SELECT e.id
        FROM langchain_pg_embedding AS e
        JOIN knowledge_documents AS d ON d.id = %(document_id)s
        JOIN brands AS b ON b.id = d.brand_id AND b.team_id = d.team_id
        WHERE e.id = %(id)s AND e.collection_id = %(collection_id)s
          AND e.cmetadata = %(metadata)s AND e.document IS NOT DISTINCT FROM %(document)s
          AND d.team_id::text = e.cmetadata ->> 'team_id'
          AND d.brand_id::text = e.cmetadata ->> 'brand_id'
          AND d.version = %(document_version)s
        FOR UPDATE OF e SKIP LOCKED
    )
    UPDATE langchain_pg_embedding AS e
    SET cmetadata = e.cmetadata || %(lexical)s
    FROM current_chunk
    WHERE e.id = current_chunk.id
"""


def backfill(database_url: str, *, batch_size: int) -> dict[str, int]:
    from app.modules.rag.retrieval.lexical import TOKENIZER_VERSION, lexical_metadata

    if batch_size < 1:
        raise ValueError("Batch size must be positive")
    conninfo = make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)
    after = ""
    selected = updated = 0
    with psycopg.connect(conninfo, autocommit=True, row_factory=dict_row, connect_timeout=10) as connection:
        while rows := connection.execute(
            _READ_BATCH, {"after": after, "version": TOKENIZER_VERSION, "limit": batch_size},
        ).fetchall():
            # Tokenize with no open transaction or chunk locks. The update
            # still requires the same row contents and canonical version.
            updates = [
                {
                    **row,
                    "metadata": Jsonb(row["cmetadata"]),
                    "lexical": Jsonb(lexical_metadata(row["document"] or "", row["cmetadata"])),
                }
                for row in rows
            ]
            with connection.transaction(), connection.cursor() as cursor:
                cursor.executemany(_UPDATE_CHUNK, updates)
                updated += cursor.rowcount
            selected += len(rows)
            after = rows[-1]["id"]
    return {"selected": selected, "updated": updated, "skipped": selected - updated}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.environ.get("RAG_LEXICAL_DATABASE_URL"))
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("Set RAG_LEXICAL_DATABASE_URL or supply --database-url explicitly")
    if args.batch_size < 1:
        parser.error("--batch-size must be positive")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        result = backfill(args.database_url, batch_size=args.batch_size)
    except Exception as exc:
        # Database exceptions may include connection credentials or row data.
        print(f"Lexical backfill stopped ({type(exc).__name__}); rerun after resolving the error.", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

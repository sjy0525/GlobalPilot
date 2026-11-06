"""Forward-only upgrades for existing application databases."""

from sqlalchemy import inspect, text

from app.db.team_scope_migrations import apply_team_scope_migrations


def apply_team_member_audit_migrations(conn) -> None:
    """Complete an empty legacy audit table without inventing historical data.

    Call inside the caller's transaction. Missing tables are created by the
    normal model-registration path; this upgrade only repairs an existing one.
    """
    schema = conn.execute(text("SELECT current_schema()")).scalar_one()
    table_name = "team_member_audit_logs"
    if schema is None or not inspect(conn).has_table(table_name, schema=schema):
        return

    quote = conn.dialect.identifier_preparer.quote
    table = f"{quote(schema)}.{quote(table_name)}"
    conn.execute(text(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE"))
    columns = {
        column["name"]: column
        for column in inspect(conn).get_columns(table_name, schema=schema)
    }
    definitions = {
        "team_id": "UUID NOT NULL",
        "actor_user_id": "UUID NOT NULL",
        "target_user_id": "UUID NOT NULL",
        "action": "VARCHAR(100) NOT NULL",
        "create_at": "TIMESTAMPTZ NOT NULL DEFAULT now()",
    }
    missing = set(definitions) - set(columns)
    if missing and conn.execute(text(f"SELECT EXISTS (SELECT 1 FROM {table})")).scalar_one():
        raise RuntimeError(
            "Cannot upgrade a nonempty incomplete team_member_audit_logs table; "
            "historical identity and event fields must be recovered explicitly"
        )

    for name, definition in definitions.items():
        if name not in columns:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {quote(name)} {definition}"))
        elif columns[name]["nullable"]:
            # Existing NULLs fail transactionally instead of being fabricated.
            conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN {quote(name)} SET NOT NULL"))
    conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN create_at SET DEFAULT now()"))

    # Historical events retain NULL snapshots; their old state is not inferred.
    for name in ("before_role", "after_role", "before_status", "after_status"):
        conn.execute(text(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {quote(name)} VARCHAR(50)"
        ))

    foreign_keys = inspect(conn).get_foreign_keys(table_name, schema=schema)
    for column, target in (
        ("team_id", "teams"),
        ("actor_user_id", "users"),
        ("target_user_id", "users"),
    ):
        existing = [fk for fk in foreign_keys if fk["constrained_columns"] == [column]]
        if existing:
            if any(
                fk["referred_table"] != target
                or fk["referred_schema"] not in (None, schema)
                or fk["referred_columns"] != ["id"]
                or fk.get("options", {}).get("ondelete") != "RESTRICT"
                for fk in existing
            ):
                raise RuntimeError(
                    f"Unexpected foreign key on team_member_audit_logs.{column}; "
                    "refusing to replace an existing constraint automatically"
                )
            continue
        constraint = quote(f"fk_team_member_audit_logs_{column}")
        reference = f"{quote(schema)}.{quote(target)}"
        conn.execute(text(
            f"ALTER TABLE {table} ADD CONSTRAINT {constraint} "
            f"FOREIGN KEY ({quote(column)}) REFERENCES {reference} (id) ON DELETE RESTRICT"
        ))


def apply_ticket_migrations(conn) -> None:
    """Add optional result/review fields without inventing historical values."""
    schema = conn.execute(text("SELECT current_schema()")).scalar_one()
    if schema is None or not inspect(conn).has_table("tickets", schema=schema):
        return

    quote = conn.dialect.identifier_preparer.quote
    table = f"{quote(schema)}.{quote('tickets')}"
    conn.execute(text(
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS processing_result TEXT"
    ))
    conn.execute(text(
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS review_comment TEXT"
    ))


def apply_concurrency_migrations(conn) -> None:
    conn.execute(text(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
        "is_active BOOLEAN NOT NULL DEFAULT TRUE"
    ))
    conn.execute(text(
        "ALTER TABLE briefs ADD COLUMN IF NOT EXISTS "
        "workflow_version BIGINT NOT NULL DEFAULT 0"
    ))
    conn.execute(text(
        "ALTER TABLE briefs ADD COLUMN IF NOT EXISTS "
        "workflow_checkpoint_id VARCHAR(128)"
    ))
    conn.execute(text(
        "ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS "
        "version INTEGER NOT NULL DEFAULT 1"
    ))
    apply_team_scope_migrations(conn)
    apply_team_member_audit_migrations(conn)
    apply_ticket_migrations(conn)

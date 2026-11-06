"""Add explicit team ownership without guessing ownership of historical rows."""

from sqlalchemy import inspect, text


def apply_team_scope_migrations(conn) -> None:
    schema = conn.execute(text("SELECT current_schema()")).scalar_one()
    if schema is None:
        raise RuntimeError("A current schema is required for team migrations")
    quote = conn.dialect.identifier_preparer.quote

    def table(name):
        return f"{quote(schema)}.{quote(name)}"

    present = {
        name for name in ("brands", "briefs", "knowledge_documents", "eval_runs")
        if inspect(conn).has_table(name, schema=schema)
    }
    if not present:
        return
    if not inspect(conn).has_table("teams", schema=schema):
        raise RuntimeError("Create teams before applying team ownership migrations")

    def ensure_foreign_key(name, columns, target, target_columns, constraint):
        existing = [fk for fk in inspect(conn).get_foreign_keys(name, schema=schema)
                    if fk["constrained_columns"] == list(columns)]
        if existing:
            if any(fk["referred_table"] != target
                   or fk["referred_schema"] not in (None, schema)
                   or fk["referred_columns"] != list(target_columns)
                   or fk.get("options", {}).get("ondelete") != "RESTRICT"
                   for fk in existing):
                raise RuntimeError(f"Unexpected team foreign key on {name}; inspect it explicitly")
            return
        cols = ", ".join(map(quote, columns))
        refs = ", ".join(map(quote, target_columns))
        conn.execute(text(
            f"ALTER TABLE {table(name)} ADD CONSTRAINT {quote(constraint)} "
            f"FOREIGN KEY ({cols}) REFERENCES {table(target)} ({refs}) ON DELETE RESTRICT"
        ))

    for name in sorted(present):
        # No default and no inferred backfill: legacy rows remain inaccessible.
        conn.execute(text(f"ALTER TABLE {table(name)} ADD COLUMN IF NOT EXISTS team_id UUID"))
        ensure_foreign_key(name, ("team_id",), "teams", ("id",), f"fk_{name}_team_id")
        conn.execute(text(
            f"CREATE INDEX IF NOT EXISTS {quote('ix_' + name + '_team_id')} "
            f"ON {table(name)} (team_id)"
        ))

    if "brands" in present:
        constraints = inspect(conn).get_unique_constraints("brands", schema=schema)
        for constraint in constraints:
            if constraint["column_names"] == ["name"]:
                conn.execute(text(
                    f"ALTER TABLE {table('brands')} DROP CONSTRAINT {quote(constraint['name'])}"
                ))
        for index in inspect(conn).get_indexes("brands", schema=schema):
            if index.get("unique") and index["column_names"] == ["name"] and not index.get("duplicates_constraint"):
                conn.execute(text(f"DROP INDEX {quote(schema)}.{quote(index['name'])}"))
        uniques = {tuple(item["column_names"]) for item in
                   inspect(conn).get_unique_constraints("brands", schema=schema)}
        for columns, constraint in (
            (("team_id", "name"), "uq_brands_team_name"),
            (("team_id", "id"), "uq_brands_team_id_id"),
        ):
            if columns not in uniques:
                conn.execute(text(
                    f"ALTER TABLE {table('brands')} ADD CONSTRAINT {quote(constraint)} "
                    f"UNIQUE ({', '.join(map(quote, columns))})"
                ))
        for name in ("briefs", "knowledge_documents"):
            if name in present:
                ensure_foreign_key(
                    name, ("team_id", "brand_id"), "brands", ("team_id", "id"),
                    f"fk_{name}_team_brand",
                )

    if "eval_runs" in present:
        # Normalize independently inside each team, including the legacy NULL scope.
        conn.execute(text(f"""
            WITH ranked AS (
                SELECT id, row_number() OVER (
                    PARTITION BY team_id, user_id, test_set_id
                    ORDER BY (status = 'completed') DESC,
                             finished_at DESC NULLS LAST, created_at DESC, id DESC
                ) AS position
                FROM {table('eval_runs')} WHERE is_baseline
            )
            UPDATE {table('eval_runs')} SET is_baseline = FALSE
            FROM ranked WHERE eval_runs.id = ranked.id AND ranked.position > 1
        """))
        indexes = {index["name"]: index for index in
                   inspect(conn).get_indexes("eval_runs", schema=schema)}

        def valid_index(index, columns, predicate):
            actual = str(index.get("dialect_options", {}).get("postgresql_where", ""))
            normalize = lambda value: "".join(value.lower().split()).replace("(", "").replace(")", "")
            return (index.get("unique") is True
                    and index["column_names"] == columns
                    and normalize(actual) == normalize(predicate))

        old = indexes.get("uq_eval_baseline")
        if old and old["column_names"] == ["user_id", "test_set_id"]:
            if not valid_index(old, ["user_id", "test_set_id"], "is_baseline"):
                raise RuntimeError("Unexpected uq_eval_baseline index; inspect before migrating")
            conn.execute(text(f"DROP INDEX {quote(schema)}.uq_eval_baseline"))
        elif old and not valid_index(
            old, ["team_id", "user_id", "test_set_id"], "is_baseline AND team_id IS NOT NULL"
        ):
            raise RuntimeError("Unexpected uq_eval_baseline index; inspect before migrating")
        legacy = indexes.get("uq_eval_baseline_legacy")
        if legacy and not valid_index(
            legacy, ["user_id", "test_set_id"], "is_baseline AND team_id IS NULL"
        ):
            raise RuntimeError("Unexpected uq_eval_baseline_legacy index; inspect before migrating")
        conn.execute(text(
            f"CREATE UNIQUE INDEX IF NOT EXISTS uq_eval_baseline ON {table('eval_runs')} "
            "(team_id, user_id, test_set_id) WHERE is_baseline AND team_id IS NOT NULL"
        ))
        conn.execute(text(
            f"CREATE UNIQUE INDEX IF NOT EXISTS uq_eval_baseline_legacy ON {table('eval_runs')} "
            "(user_id, test_set_id) WHERE is_baseline AND team_id IS NULL"
        ))

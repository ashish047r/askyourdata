"""Creates the read-only role used for LLM-generated SQL and turns on row-level security.

The role name/password come from DATABASE_URL_RO, so there is one source of truth.
If your host refuses CREATE ROLE from SQL, create the role in its console with the same
name/password and re-run `python manage.py migrate` (the grants and policies still apply)."""

import os

import dj_database_url
from django.db import migrations
from psycopg import sql

TABLES = ("ga4_daily", "ads_campaign_daily")


def forwards(apps, schema_editor):
    ro = dj_database_url.parse(os.environ["DATABASE_URL_RO"])
    conn = schema_editor.connection
    role = sql.Identifier(ro["USER"])
    with conn.cursor() as c:
        c.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [ro["USER"]])
        if not c.fetchone():
            c.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(role, sql.Literal(ro["PASSWORD"]))
                      .as_string(conn.connection))
        statements = [
            sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role),
            sql.SQL("GRANT TEMPORARY ON DATABASE {} TO {}").format(sql.Identifier(conn.settings_dict["NAME"]), role),
            sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {}").format(role),
            sql.SQL("GRANT SELECT ON ga4_daily, ads_campaign_daily TO {}").format(role),
        ]
        # The tenant id lives in a per-transaction temp table created by agent/core/executor.py.
        # Unlike a set_config() setting, a SELECT cannot change it: data-modifying CTEs are invisible to
        # the rest of the same statement, and only one LLM statement runs per transaction.
        statements.append(sql.SQL(
            "CREATE OR REPLACE FUNCTION app_tenant_id() RETURNS bigint LANGUAGE plpgsql STABLE AS "
            "$$ BEGIN RETURN (SELECT id FROM pg_temp.tenant_scope); END $$"))
        for t in TABLES:
            table = sql.Identifier(t)
            statements += [
                sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(table),
                sql.SQL("DROP POLICY IF EXISTS tenant_isolation ON {}").format(table),
                # If tenant_scope doesn't exist the function errors: fail closed.
                sql.SQL("CREATE POLICY tenant_isolation ON {} FOR SELECT TO {} "
                        "USING (client_id = app_tenant_id())").format(table, role),
            ]
        for stmt in statements:
            c.execute(stmt.as_string(conn.connection))


def backwards(apps, schema_editor):
    with schema_editor.connection.cursor() as c:
        for t in TABLES:
            c.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {t}")
            c.execute(f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY")
        c.execute("DROP FUNCTION IF EXISTS app_tenant_id()")


class Migration(migrations.Migration):
    dependencies = [("data", "0001_initial")]
    operations = [migrations.RunPython(forwards, backwards)]

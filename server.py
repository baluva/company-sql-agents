"""MCP server exposing a company SQL Server database to Claude agents.

Safety model
- Reads go through a read-only login, and every query is parsed with sqlglot
  so only a single SELECT statement is ever sent.
- Writes are two-step: preview_write runs the statement inside a transaction
  that is rolled back and reports what would change; execute_write replays it
  only if the affected row count still matches, then logs it to an audit table.
- Only tables listed in WRITABLE_TABLES can be modified, and UPDATE/DELETE
  without a WHERE clause are refused.
"""

from __future__ import annotations

import csv
import datetime as dt
import decimal
import io
import json
import os
import re
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Literal

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter
import pymssql
import sqlglot
from dotenv import load_dotenv
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from sqlglot import exp

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "1433"))
DB_NAME = os.getenv("DB_NAME", "CompanyDB")
MAX_ROWS = int(os.getenv("MAX_ROWS", "500"))
QUERY_TIMEOUT = int(os.getenv("QUERY_TIMEOUT_SECONDS", "30"))
WRITABLE_TABLES = {
    t.strip().lower() for t in os.getenv("WRITABLE_TABLES", "").split(",") if t.strip()
}
REPORTS_DIR = ROOT / "reports"
PENDING_TTL_SECONDS = 15 * 60

mcp = MCPServer(
    name="company-sql",
    instructions=(
        "Tools for the company SQL Server database (T-SQL dialect). "
        "Call list_tables and describe_table before writing queries; never guess column names. "
        "Use TOP instead of LIMIT. To change data, call preview_write first, show the user "
        "the preview, and only call execute_write after they explicitly approve."
    ),
)


# ---------------------------------------------------------------- connection

def _connect(role: Literal["reader", "writer"]) -> pymssql.Connection:
    user = os.getenv(f"DB_{role.upper()}_USER")
    password = os.getenv(f"DB_{role.upper()}_PASSWORD")
    if not user:
        raise ToolError(f"No {role} login configured (DB_{role.upper()}_USER is empty).")
    return pymssql.connect(
        server=DB_HOST,
        port=DB_PORT,
        user=user,
        password=password,
        database=DB_NAME,
        login_timeout=10,
        timeout=QUERY_TIMEOUT,
        as_dict=True,
        autocommit=False,
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (dt.date, dt.datetime, dt.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _db_message(e: pymssql.Error) -> str:
    """The SQL Server message without the DB-Lib boilerplate."""
    raw = e.args[0] if e.args else e
    if isinstance(raw, tuple) and len(raw) > 1:
        raw = raw[1]
    elif len(e.args) > 1:
        raw = e.args[1]
    if isinstance(raw, bytes):
        raw = raw.decode(errors="replace")
    return str(raw).split("DB-Lib error")[0].strip()


def _fetch(sql: str, params: tuple = (), limit: int = MAX_ROWS) -> tuple[list[dict], bool]:
    """Run a read query as the reader login. Returns (rows, truncated)."""
    try:
        with closing(_connect("reader")) as conn, closing(conn.cursor()) as cur:
            cur.execute(sql, params or None)
            rows = cur.fetchmany(limit + 1)
    except pymssql.Error as e:
        raise ToolError(f"SQL Server error: {_db_message(e)}") from e
    truncated = len(rows) > limit
    rows = [{k: _jsonable(v) for k, v in r.items()} for r in rows[:limit]]
    return rows, truncated


# ---------------------------------------------------------------- validation

_FORBIDDEN_IN_READ = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop,
    exp.Alter, exp.Command, exp.Into, exp.TruncateTable,
)


def _parse_single(sql: str) -> exp.Expression:
    try:
        statements = [s for s in sqlglot.parse(sql, read="tsql") if s is not None]
    except sqlglot.errors.ParseError as e:
        raise ToolError(f"Could not parse SQL: {e}") from e
    if len(statements) != 1:
        raise ToolError("Send exactly one SQL statement.")
    return statements[0]


def _validate_select(sql: str) -> None:
    tree = _parse_single(sql)
    if not isinstance(tree, exp.Query):
        raise ToolError("Only SELECT queries are allowed here. Use preview_write for changes.")
    if any(tree.find_all(*_FORBIDDEN_IN_READ)):
        raise ToolError("Query contains a data-modifying clause (e.g. SELECT INTO).")


def _qualified(table: exp.Table) -> str:
    schema = table.db or "dbo"
    return f"{schema}.{table.name}".lower()


def _validate_write(sql: str) -> tuple[exp.Expression, str]:
    tree = _parse_single(sql)
    if not isinstance(tree, (exp.Insert, exp.Update, exp.Delete)):
        raise ToolError("Only a single INSERT, UPDATE or DELETE statement is allowed.")
    target = tree.this if isinstance(tree.this, exp.Table) else tree.find(exp.Table)
    if target is None:
        raise ToolError("Could not find the target table.")
    table = _qualified(target)
    if table not in WRITABLE_TABLES:
        allowed = ", ".join(sorted(WRITABLE_TABLES)) or "none"
        raise ToolError(f"Table {table} is not writable. Writable tables: {allowed}.")
    if isinstance(tree, (exp.Update, exp.Delete)) and not tree.args.get("where"):
        raise ToolError("UPDATE and DELETE must have a WHERE clause.")
    return tree, table


# ---------------------------------------------------------------- schema tools

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)


@mcp.tool(annotations=READ_ONLY)
def list_tables() -> list[dict]:
    """List every table in the database with its schema and approximate row count."""
    rows, _ = _fetch(
        """
        SELECT s.name AS [schema], t.name AS [table], SUM(p.rows) AS row_count,
               CAST(ep.value AS NVARCHAR(400)) AS description
        FROM sys.tables t
        JOIN sys.schemas s ON s.schema_id = t.schema_id
        JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0, 1)
        LEFT JOIN sys.extended_properties ep
               ON ep.major_id = t.object_id AND ep.minor_id = 0 AND ep.name = 'MS_Description'
        GROUP BY s.name, t.name, ep.value
        ORDER BY s.name, t.name
        """
    )
    for r in rows:
        r["writable"] = f"{r['schema']}.{r['table']}".lower() in WRITABLE_TABLES
    return rows


@mcp.tool(annotations=READ_ONLY)
def describe_table(table: str) -> dict:
    """Columns, primary key, foreign keys and 5 sample rows for a table.

    Args:
        table: Table name as schema.table (e.g. sales.orders). Schema defaults to dbo.
    """
    schema, _, name = table.rpartition(".")
    schema = schema or "dbo"
    if not re.fullmatch(r"\w+", schema) or not re.fullmatch(r"\w+", name):
        raise ToolError("Table must look like schema.table.")

    columns, _ = _fetch(
        """
        SELECT c.COLUMN_NAME AS name, c.DATA_TYPE AS type, c.CHARACTER_MAXIMUM_LENGTH AS max_length,
               c.IS_NULLABLE AS nullable, c.COLUMN_DEFAULT AS default_value
        FROM INFORMATION_SCHEMA.COLUMNS c
        WHERE c.TABLE_SCHEMA = %s AND c.TABLE_NAME = %s
        ORDER BY c.ORDINAL_POSITION
        """,
        (schema, name),
    )
    if not columns:
        raise ToolError(f"Table {schema}.{name} not found. Call list_tables to see what exists.")

    pk, _ = _fetch(
        """
        SELECT k.COLUMN_NAME AS name
        FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
        JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE k
          ON k.CONSTRAINT_NAME = tc.CONSTRAINT_NAME AND k.TABLE_SCHEMA = tc.TABLE_SCHEMA
        WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY' AND tc.TABLE_SCHEMA = %s AND tc.TABLE_NAME = %s
        """,
        (schema, name),
    )
    fks, _ = _fetch(
        """
        SELECT pc.name AS [column], rs.name + '.' + rt.name AS references_table, rc.name AS references_column
        FROM sys.foreign_key_columns fkc
        JOIN sys.tables pt ON pt.object_id = fkc.parent_object_id
        JOIN sys.schemas ps ON ps.schema_id = pt.schema_id
        JOIN sys.columns pc ON pc.object_id = fkc.parent_object_id AND pc.column_id = fkc.parent_column_id
        JOIN sys.tables rt ON rt.object_id = fkc.referenced_object_id
        JOIN sys.schemas rs ON rs.schema_id = rt.schema_id
        JOIN sys.columns rc ON rc.object_id = fkc.referenced_object_id AND rc.column_id = fkc.referenced_column_id
        WHERE ps.name = %s AND pt.name = %s
        """,
        (schema, name),
    )
    sample, _ = _fetch(f"SELECT TOP 5 * FROM [{schema}].[{name}]")
    return {
        "table": f"{schema}.{name}",
        "writable": f"{schema}.{name}".lower() in WRITABLE_TABLES,
        "columns": columns,
        "primary_key": [r["name"] for r in pk],
        "foreign_keys": fks,
        "sample_rows": sample,
    }


# ---------------------------------------------------------------- query tools

@mcp.tool(annotations=READ_ONLY)
def run_query(sql: str) -> dict:
    """Run one read-only T-SQL SELECT and return the rows (capped at MAX_ROWS).

    Args:
        sql: A single SELECT statement in T-SQL (use TOP, not LIMIT).
    """
    _validate_select(sql)
    started = time.perf_counter()
    rows, truncated = _fetch(sql)
    return {
        "row_count": len(rows),
        "truncated": truncated,
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
        "rows": rows,
    }


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False))
def create_chart(
    sql: str,
    x: str,
    y: list[str],
    title: str,
    chart_type: Literal["bar", "barh", "line", "pie"] = "bar",
) -> list:
    """Run a SELECT and draw its result as a chart, saved as PNG in reports/.

    Args:
        sql: SELECT returning the x column and one or more numeric y columns.
        x: Column used for categories or the time axis.
        y: Numeric column(s) to plot. Pie charts use only the first.
        title: Chart title stating the takeaway, e.g. "Revenue doubled in Q3".
        chart_type: bar, barh (long category labels), line (time series) or pie (<= 6 parts).
    """
    _validate_select(sql)
    rows, truncated = _fetch(sql)
    if not rows:
        raise ToolError("The query returned no rows.")
    missing = [c for c in [x, *y] if c not in rows[0]]
    if missing:
        raise ToolError(f"Columns not in result: {missing}. Available: {list(rows[0])}")

    labels = [str(r[x]) for r in rows]
    fig, ax = plt.subplots(figsize=(10, 5.5), dpi=120)
    palette = ["#1f4e79", "#c55a11", "#548235", "#7f6000", "#7030a0"]

    if chart_type == "pie":
        ax.pie([r[y[0]] or 0 for r in rows], labels=labels, autopct="%1.0f%%",
               colors=palette + ["#999999"] * len(rows), startangle=90, counterclock=False)
        ax.axis("equal")
    else:
        width = 0.8 / len(y)
        for i, col in enumerate(y):
            values = [r[col] or 0 for r in rows]
            if chart_type == "line":
                ax.plot(labels, values, marker="o", linewidth=2, color=palette[i % 5], label=col)
            elif chart_type == "barh":
                pos = [j + i * width for j in range(len(rows))]
                ax.barh(pos, values, height=width, color=palette[i % 5], label=col)
                ax.set_yticks([j + width * (len(y) - 1) / 2 for j in range(len(rows))], labels)
                ax.invert_yaxis()
            else:
                pos = [j + i * width for j in range(len(rows))]
                ax.bar(pos, values, width=width, color=palette[i % 5], label=col)
                ax.set_xticks([j + width * (len(y) - 1) / 2 for j in range(len(rows))], labels)
        if len(labels) > 8 and chart_type != "barh":
            plt.setp(ax.get_xticklabels(), rotation=45, ha="right")
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        ax.grid(axis="x" if chart_type == "barh" else "y", alpha=0.3)
        value_axis = ax.xaxis if chart_type == "barh" else ax.yaxis
        value_axis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
        if len(y) > 1:
            ax.legend(frameon=False)

    ax.set_title(title, loc="left", fontsize=13, fontweight="bold")
    fig.tight_layout()

    REPORTS_DIR.mkdir(exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:50] or "chart"
    path = REPORTS_DIR / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{slug}.png"
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    path.write_bytes(buf.getvalue())

    note = f"Chart saved to {path}" + (" (data truncated to MAX_ROWS)" if truncated else "")
    return [note, Image(data=buf.getvalue(), format="png")]


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False))
def export_csv(sql: str, filename: str) -> str:
    """Run a SELECT and save the full result (up to 100,000 rows) as CSV in reports/.

    Args:
        sql: A single SELECT statement.
        filename: File name without extension.
    """
    _validate_select(sql)
    rows, truncated = _fetch(sql, limit=100_000)
    REPORTS_DIR.mkdir(exist_ok=True)
    safe = re.sub(r"[^\w-]+", "_", filename)[:80] or "export"
    path = REPORTS_DIR / f"{safe}.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        if rows:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    return f"Saved {len(rows)} rows to {path}" + (" (truncated at 100,000)" if truncated else "")


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False))
def save_report(title: str, markdown: str) -> str:
    """Save a finished Markdown report in reports/. Reference charts by their saved file name.

    Args:
        title: Report title.
        markdown: Full report body in Markdown.
    """
    REPORTS_DIR.mkdir(exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "report"
    path = REPORTS_DIR / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{slug}.md"
    path.write_text(f"# {title}\n\n_Generated {dt.datetime.now():%Y-%m-%d %H:%M}_\n\n{markdown}\n", encoding="utf-8")
    return f"Report saved to {path}"


# ---------------------------------------------------------------- write tools

_pending: dict[str, dict] = {}


def _affected_preview(tree: exp.Expression) -> list[dict]:
    """Rows an UPDATE/DELETE would touch, read before the change."""
    if not isinstance(tree, (exp.Update, exp.Delete)):
        return []
    target = tree.this if isinstance(tree.this, exp.Table) else tree.find(exp.Table)
    select = exp.select("*").from_(target.copy()).where(tree.args["where"].this.copy()).limit(20)
    rows, _ = _fetch(select.sql(dialect="tsql"), limit=20)
    return rows


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False))
def preview_write(sql: str, reason: str) -> dict:
    """Dry-run an INSERT, UPDATE or DELETE: it runs inside a transaction that is rolled back.

    Returns a write_id, the number of rows that would change and (for UPDATE/DELETE)
    the current values of up to 20 affected rows. Show this to the user and wait for
    explicit approval before calling execute_write.

    Args:
        sql: A single INSERT, UPDATE or DELETE on a writable table.
        reason: Why this change is needed, in one sentence. Stored in the audit log.
    """
    tree, table = _validate_write(sql)
    before = _affected_preview(tree)
    with closing(_connect("writer")) as conn, closing(conn.cursor()) as cur:
        try:
            cur.execute(sql)
            affected = cur.rowcount
        except pymssql.Error as e:
            raise ToolError(f"SQL Server error: {_db_message(e)}") from e
        finally:
            conn.rollback()

    now = time.time()
    for key in [k for k, v in _pending.items() if v["expires"] < now]:
        del _pending[key]
    write_id = uuid.uuid4().hex[:8]
    _pending[write_id] = {
        "sql": sql, "table": table, "reason": reason,
        "affected": affected, "expires": now + PENDING_TTL_SECONDS,
    }
    return {
        "write_id": write_id,
        "operation": type(tree).__name__.upper(),
        "table": table,
        "rows_affected": affected,
        "current_values_of_affected_rows": before,
        "expires_in_minutes": PENDING_TTL_SECONDS // 60,
        "next_step": "Show this preview to the user. Call execute_write only after they approve.",
    }


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False))
def execute_write(write_id: str, approved_by: str) -> dict:
    """Apply a write previously returned by preview_write, after the user approved it.

    The change is committed only if it affects the same number of rows as the preview;
    otherwise it is rolled back. Every committed write is recorded in audit.agent_write_log.

    Args:
        write_id: The id returned by preview_write.
        approved_by: Name of the person who approved the change.
    """
    pending = _pending.pop(write_id, None)
    if pending is None or pending["expires"] < time.time():
        raise ToolError("Unknown or expired write_id. Run preview_write again.")

    with closing(_connect("writer")) as conn, closing(conn.cursor()) as cur:
        try:
            cur.execute(pending["sql"])
            affected = cur.rowcount
            if affected != pending["affected"]:
                conn.rollback()
                raise ToolError(
                    f"Data changed since the preview ({pending['affected']} rows expected, "
                    f"{affected} now). Rolled back; preview again."
                )
            cur.execute(
                "INSERT INTO audit.agent_write_log (table_name, sql_text, reason, rows_affected, approved_by) "
                "VALUES (%s, %s, %s, %s, %s)",
                (pending["table"], pending["sql"], pending["reason"], affected, approved_by),
            )
            conn.commit()
        except pymssql.Error as e:
            conn.rollback()
            raise ToolError(f"SQL Server error, nothing was changed: {_db_message(e)}") from e
        except Exception:
            conn.rollback()
            raise
    return {"status": "committed", "table": pending["table"], "rows_affected": affected}


if __name__ == "__main__":
    mcp.run()

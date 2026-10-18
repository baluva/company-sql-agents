"""End-to-end check: start server.py over stdio like Claude does and call every tool.

Run:  uv run scripts/smoke_test.py
"""

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[1]
failures = 0


def text_of(result) -> str:
    return "\n".join(getattr(c, "text", f"<{c.type}>") for c in result.content)


async def check(session, name, args, expect_error=False, show=True):
    global failures
    result = await session.call_tool(name, args)
    ok = bool(result.is_error) == expect_error
    failures += not ok
    label = "blocked as expected" if expect_error and ok else "ok" if ok else "UNEXPECTED"
    body = text_of(result)
    print(f"[{label}] {name} {json.dumps(args)[:90]}")
    if show or not ok:
        print("   ", body[:300].replace("\n", "\n    "))
    return result


async def main():
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "server.py")], cwd=str(ROOT))
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        print("Tools:", ", ".join(t.name for t in tools.tools), "\n")

        await check(session, "list_tables", {}, show=False)
        await check(session, "describe_table", {"table": "sales.order_items"}, show=False)
        await check(session, "run_query", {"sql": """
            SELECT TOP 5 p.category,
                   CAST(SUM(oi.quantity * oi.unit_price * (1 - oi.discount)) AS DECIMAL(12,2)) AS revenue
            FROM sales.order_items oi JOIN inventory.products p ON p.product_id = oi.product_id
            GROUP BY p.category ORDER BY revenue DESC"""})

        # read-path guards
        await check(session, "run_query", {"sql": "DELETE FROM sales.orders"}, expect_error=True)
        await check(session, "run_query", {"sql": "SELECT 1; DROP TABLE sales.orders"}, expect_error=True)
        await check(session, "run_query", {"sql": "SELECT * INTO dbo.copy FROM sales.orders"}, expect_error=True)
        await check(session, "run_query", {"sql": "SELECT revenue FROM sales.orders"}, expect_error=True)

        await check(session, "create_chart", {
            "sql": """SELECT FORMAT(o.order_date, 'yyyy-MM') AS month,
                             SUM(oi.quantity * oi.unit_price * (1 - oi.discount)) AS revenue
                      FROM sales.orders o JOIN sales.order_items oi ON oi.order_id = o.order_id
                      WHERE o.status <> 'Cancelled' AND o.order_date >= '2025-01-01'
                      GROUP BY FORMAT(o.order_date, 'yyyy-MM') ORDER BY month""",
            "x": "month", "y": ["revenue"], "chart_type": "line",
            "title": "Monthly revenue since January 2025"})

        # write-path guards
        await check(session, "preview_write", {"sql": "UPDATE sales.customers SET is_active = 0", "reason": "t"}, expect_error=True)
        await check(session, "preview_write", {"sql": "DELETE FROM hr.employees WHERE employee_id = 1", "reason": "t"}, expect_error=True)
        await check(session, "execute_write", {"write_id": "deadbeef", "approved_by": "test"}, expect_error=True)

        # full two-step write
        preview = await check(session, "preview_write", {
            "sql": "UPDATE inventory.products SET reorder_level = 60 WHERE category = 'Tents'",
            "reason": "Smoke test: raise tent reorder level before summer"})
        write_id = json.loads(text_of(preview))["write_id"]
        await check(session, "execute_write", {"write_id": write_id, "approved_by": "smoke test"})
        await check(session, "execute_write", {"write_id": write_id, "approved_by": "smoke test"}, expect_error=True)
        await check(session, "run_query", {"sql": "SELECT TOP 1 table_name, rows_affected, approved_by, reason FROM audit.agent_write_log ORDER BY log_id DESC"})

    print(f"\n{'All checks passed' if not failures else f'{failures} check(s) failed'}")
    sys.exit(1 if failures else 0)


asyncio.run(main())


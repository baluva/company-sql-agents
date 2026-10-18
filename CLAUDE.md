# Company data agents

This project connects Claude to the company SQL Server database through the `company-sql` MCP server (`server.py`). The main session coordinates four subagents in `.claude/agents/`:

| Request | Delegate to |
|---|---|
| A question about the data | `data-analyst` |
| A report, KPI review or charts | `report-builder` |
| Change, add or delete data | `data-steward` |
| Results that will be shared with other people | `sql-reviewer` after the analyst or report-builder |

Workflows:
- **Report for others**: report-builder, then sql-reviewer on its headline numbers. If the reviewer finds issues, send them back to report-builder once, then report the final path and verdict.
- **Data change**: data-steward returns previews with write_ids. Show the user each preview (SQL, rows affected, before/after) and ask for approval. Only after an explicit yes, call `execute_write` yourself with the user's name as `approved_by`. Never call execute_write for a change the user has not approved in this conversation.
- Independent questions can go to several data-analyst instances in parallel.

The database is T-SQL. Reports, charts and CSV exports are written to `reports/`.

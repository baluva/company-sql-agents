# Company SQL agents

Claude agents that work on a company SQL Server database through a custom MCP server: they answer questions, build reports with charts, and make data changes with a preview, human approval and an audit log.

```
 Claude Code / Claude Desktop
   ├─ main session (routing rules in CLAUDE.md, applies approved writes)
   ├─ data-analyst   ─┐
   ├─ report-builder  ├──>  MCP server "company-sql" (server.py)  ──>  SQL Server
   ├─ data-steward    │       reader login: SELECT only
   └─ sql-reviewer   ─┘       writer login: listed tables + audit log
```

## MCP tools

| Tool | What it does |
|---|---|
| `list_tables`, `describe_table` | Schema, row counts, keys, table descriptions, sample rows |
| `run_query` | One SELECT, parsed with sqlglot, run as the read-only login, capped at `MAX_ROWS` |
| `create_chart` | SELECT, then a PNG chart in `reports/`, also returned to Claude as an image |
| `export_csv`, `save_report` | Write CSV / Markdown reports to `reports/` |
| `preview_write` | Dry-runs an INSERT/UPDATE/DELETE in a rolled-back transaction, returns a `write_id` |
| `execute_write` | Applies a previewed write if the row count still matches, logs it in `audit.agent_write_log` |

Guardrails, from strongest to weakest: database permissions (the reader login cannot write at all), the `WRITABLE_TABLES` allowlist, the SQL parser (single statement, no `SELECT INTO`, no UPDATE/DELETE without WHERE), the two-step write with a row-count check, and Claude Code asking before every `execute_write` (`.claude/settings.json`).

## Run the demo

Needs Docker and [uv](https://docs.astral.sh/uv/).

```powershell
copy .env.example .env          # then change the passwords
docker compose up -d            # SQL Server 2022 Developer on port 1433
uv run scripts/seed.py          # demo outdoor-gear retailer: 120 customers, ~770 orders, 2024-2026
uv run scripts/smoke_test.py    # calls every tool over MCP, including the blocked cases
claude                          # accept the trust dialog and approve the company-sql server once
```

Then ask, for example:
- "Which customers haven't ordered in 6 months?"
- "Build the September 2026 sales report and have it reviewed."
- "Products below reorder level: raise their stock to 2x the reorder level."

## Connect the real company database

1. Ask the DBA to run `scripts/create_logins.sql` (two least-privilege logins plus the audit table). Leave `DB_WRITER_USER` empty for read-only agents.
2. Put the host, database and logins in `.env`, and set `WRITABLE_TABLES`.
3. Add descriptions to important tables (`MS_Description` extended property). The agents read them and follow the business definitions.
4. Run `uv run scripts/smoke_test.py` against a test copy before production. Its write test updates `inventory.products`, so adapt or skip it.

The company data goes to Claude in tool results, so check that your company's policy allows it (Claude for Work / API terms, data residency) before connecting production data.

## Claude Desktop

Add this to `%APPDATA%\Claude\claude_desktop_config.json`, then restart Claude Desktop:

```json
{
  "mcpServers": {
    "company-sql": {
      "command": "uv",
      "args": ["run", "--project", "C:\\Users\\louey\\company-sql-agents", "python", "C:\\Users\\louey\\company-sql-agents\\server.py"]
    }
  }
}
```

The subagents are a Claude Code feature. In Claude Desktop you get the same tools in a single conversation.

-- For the company DBA: the two least-privilege logins the MCP server needs.
-- Replace <DB>, passwords, and the GRANT list with the tables agents may change.

USE master;
CREATE LOGIN agent_reader WITH PASSWORD = '<strong password>';
CREATE LOGIN agent_writer WITH PASSWORD = '<strong password>';  -- skip if agents must stay read-only

USE [<DB>];
CREATE USER agent_reader FOR LOGIN agent_reader;
ALTER ROLE db_datareader ADD MEMBER agent_reader;
-- Narrower alternative to db_datareader: GRANT SELECT ON SCHEMA::sales TO agent_reader;
-- Hide sensitive columns:               DENY SELECT ON hr.employees(salary) TO agent_reader;

CREATE USER agent_writer FOR LOGIN agent_writer;
ALTER ROLE db_datareader ADD MEMBER agent_writer;
GRANT INSERT, UPDATE, DELETE ON sales.customers TO agent_writer;   -- one line per writable table

-- Audit table for agent writes
CREATE SCHEMA audit;
GO
CREATE TABLE audit.agent_write_log (
    log_id INT IDENTITY PRIMARY KEY,
    executed_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    table_name NVARCHAR(128) NOT NULL,
    sql_text NVARCHAR(MAX) NOT NULL,
    reason NVARCHAR(400) NOT NULL,
    rows_affected INT NOT NULL,
    approved_by NVARCHAR(100) NOT NULL
);
GRANT INSERT ON audit.agent_write_log TO agent_writer;

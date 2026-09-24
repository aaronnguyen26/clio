-- ============================================================================
-- Task Memory Engine: Production SQLite Schema (WAL Mode, FTS5 with Porter Stemming)
-- Zero 3rd-party dependencies. Pure SQLite standard library.
-- ============================================================================

PRAGMA foreign_keys = ON;

-- 1. Workflows Master Table
CREATE TABLE IF NOT EXISTS workflows (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1,
    spec_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 2. Workflow Version History (Immutable Audit Log)
CREATE TABLE IF NOT EXISTS workflow_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    change_summary TEXT,
    spec_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(workflow_id) REFERENCES workflows(id) ON DELETE CASCADE
);

-- 3. Execution Telemetry Table (no strict FK on workflow_id for ephemeral/benchmark telemetry)
CREATE TABLE IF NOT EXISTS executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL,
    status TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    steps_completed INTEGER NOT NULL,
    error_message TEXT,
    timestamp TEXT NOT NULL,
    parameters_json TEXT
);

-- 4. Performance Indexes
CREATE INDEX IF NOT EXISTS idx_workflows_active ON workflows(active);
CREATE INDEX IF NOT EXISTS idx_workflows_updated_at ON workflows(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_workflow_versions_wf_ver ON workflow_versions(workflow_id, version);
CREATE INDEX IF NOT EXISTS idx_executions_workflow_id ON executions(workflow_id);
CREATE INDEX IF NOT EXISTS idx_executions_timestamp ON executions(timestamp DESC);

-- 5. Standalone FTS5 Full-Text Search Virtual Table with Porter Stemming
CREATE VIRTUAL TABLE IF NOT EXISTS workflow_fts USING fts5(
    workflow_id UNINDEXED,
    name,
    description,
    triggers,
    tokenize='porter unicode61'
);

-- 6. Real-Time FTS Synchronization Triggers (Resilient to Corrupt JSON)
CREATE TRIGGER IF NOT EXISTS trg_workflows_ai AFTER INSERT ON workflows
BEGIN
    INSERT INTO workflow_fts(workflow_id, name, description, triggers)
    SELECT
        new.id,
        new.name,
        coalesce(new.description, ''),
        CASE
            WHEN json_valid(new.spec_json) THEN
                coalesce(json_extract(new.spec_json, '$.triggers.canonical'), '') || ' ' ||
                coalesce(json_extract(new.spec_json, '$.triggers.aliases'), '') || ' ' ||
                coalesce(json_extract(new.spec_json, '$.triggers.keywords'), '')
            ELSE ''
        END
    WHERE new.active = 1;
END;

CREATE TRIGGER IF NOT EXISTS trg_workflows_ad AFTER DELETE ON workflows
BEGIN
    DELETE FROM workflow_fts WHERE workflow_id = old.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_workflows_au AFTER UPDATE ON workflows
BEGIN
    DELETE FROM workflow_fts WHERE workflow_id = old.id;
    INSERT INTO workflow_fts(workflow_id, name, description, triggers)
    SELECT
        new.id,
        new.name,
        coalesce(new.description, ''),
        CASE
            WHEN json_valid(new.spec_json) THEN
                coalesce(json_extract(new.spec_json, '$.triggers.canonical'), '') || ' ' ||
                coalesce(json_extract(new.spec_json, '$.triggers.aliases'), '') || ' ' ||
                coalesce(json_extract(new.spec_json, '$.triggers.keywords'), '')
            ELSE ''
        END
    WHERE new.active = 1;
END;

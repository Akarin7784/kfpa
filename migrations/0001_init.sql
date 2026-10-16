-- KFPA PostgreSQL 初始化迁移（P1 / D3 §3.2）
-- 与 SQLite 侧同构：列名、顺序、语义一一对应；JSON/列表一律 TEXT（存 schemas.dumps 的稳定序列化），
-- 不用 JSONB —— 避免两个驱动在「顺序/去重」上出现行为差异（A12 双库对拍要的是同一断言集）。
-- 时间戳一律 DOUBLE PRECISION 的 Unix epoch 秒（UTC）。
-- 幂等：全部 IF NOT EXISTS，可反复执行（PgStore.init_schema 每次都会跑）。

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rounds (
    round_no   INTEGER PRIMARY KEY,
    design_ref TEXT NOT NULL,
    state      TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS tickets (
    ticket_id            TEXT PRIMARY KEY,
    round_no             INTEGER NOT NULL,
    location             TEXT NOT NULL,
    option_a             TEXT NOT NULL,
    option_b             TEXT NOT NULL,
    architect_preference TEXT NOT NULL,
    impact               TEXT NOT NULL,
    status               TEXT NOT NULL,
    answer               TEXT,
    created_at           DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tickets_round ON tickets(round_no, status, impact);

CREATE TABLE IF NOT EXISTS task_packages (
    round_no     INTEGER PRIMARY KEY,
    package_hash TEXT NOT NULL,
    payload      TEXT NOT NULL,
    created_at   DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS gate_reports (
    id                SERIAL PRIMARY KEY,
    round_no          INTEGER NOT NULL,
    attempt           INTEGER NOT NULL,
    state             TEXT NOT NULL,
    reason_key        TEXT NOT NULL DEFAULT '',
    reasons           TEXT NOT NULL,
    failing_criteria  TEXT NOT NULL,
    escalated         BOOLEAN NOT NULL DEFAULT FALSE,
    same_reason_count INTEGER NOT NULL DEFAULT 0,
    created_at        DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_gate_round ON gate_reports(round_no, attempt);

CREATE TABLE IF NOT EXISTS failures (
    id            SERIAL PRIMARY KEY,
    round_no      INTEGER NOT NULL,
    attempt       INTEGER NOT NULL,
    reason        TEXT NOT NULL,
    avoid_pattern TEXT NOT NULL,
    reason_key    TEXT NOT NULL DEFAULT '',
    created_at    DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_failures_key ON failures(round_no, reason_key);

CREATE TABLE IF NOT EXISTS snapshots (
    round_no     INTEGER PRIMARY KEY,
    package_hash TEXT NOT NULL,
    gate_state   TEXT NOT NULL,
    git_sha      TEXT NOT NULL DEFAULT '',
    tag          TEXT NOT NULL DEFAULT '',
    created_at   DOUBLE PRECISION NOT NULL
);

-- ------------------------------------------------------------ P1
CREATE TABLE IF NOT EXISTS ledger (
    id         SERIAL PRIMARY KEY,
    round_no   INTEGER NOT NULL,
    role       TEXT NOT NULL,
    model_ref  TEXT NOT NULL DEFAULT '',
    tokens_in  INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost       DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    source     TEXT NOT NULL DEFAULT 'mock',
    created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ledger_round ON ledger(round_no, role);

CREATE TABLE IF NOT EXISTS knowledge (
    cid        TEXT PRIMARY KEY,
    round_no   INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    content    TEXT NOT NULL,
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    status     TEXT NOT NULL,
    reused_by  TEXT NOT NULL DEFAULT '[]',
    created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_knowledge_status ON knowledge(status, round_no);

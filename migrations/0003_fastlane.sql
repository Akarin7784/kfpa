-- KFPA PostgreSQL 迁移 0003（P3 D5-3.2 快车道承认落库 / D5-3.3 人工抽检留痕）
-- 与 SQLite 侧（store/sqlite.py 的 SCHEMA_SQL）同构：
--   * 布尔用 INTEGER 0/1 —— 避免两驱动 BOOLEAN/INTEGER 的绑定差异；
--   * 列表用 `schemas.dumps()` 的 TEXT（JSON），顺序稳定；
--   * 时间戳仍是 epoch 秒（DOUBLE PRECISION）。
-- 幂等：IF NOT EXISTS，可反复执行（PG 侧 init_schema 按文件名顺序全量应用）。

-- 快车道请求台账（state 机见 D5-3.2；审计三件套 = status/batch_id/decided_by+decided_at）
CREATE TABLE IF NOT EXISTS fastlane_requests (
    request_id      TEXT PRIMARY KEY,
    round_no        INTEGER NOT NULL,
    summary         TEXT NOT NULL DEFAULT '',
    diff_files      TEXT NOT NULL DEFAULT '[]',
    new_interface   INTEGER NOT NULL DEFAULT 0,
    new_data_model  INTEGER NOT NULL DEFAULT 0,
    touched_modules TEXT NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL DEFAULT 'pending',
    batch_id        TEXT NOT NULL DEFAULT '',
    decided_by      TEXT NOT NULL DEFAULT '',
    decided_at      DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    note            TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_fastlane_status ON fastlane_requests(status, round_no);

-- 人工抽检留痕（design §5.5「动态抽检」；append-only，一行一次抽检结论）
CREATE TABLE IF NOT EXISTS knowledge_samples (
    id         SERIAL PRIMARY KEY,
    cid        TEXT NOT NULL,
    sampled_at DOUBLE PRECISION NOT NULL,
    sampled_by TEXT NOT NULL DEFAULT '',
    rate       DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    passed     INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_ksamples_cid ON knowledge_samples(cid, sampled_at);

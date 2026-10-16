-- KFPA PostgreSQL 迁移 0002（P2 D4-3.3）：评审留痕表 `reviews`
-- 与 SQLite 侧同构：`parse_ok` 用 INTEGER 0/1（避免两驱动 BOOLEAN/INTEGER 的绑定差异），
-- `findings` 存 schemas.dumps() 的 TEXT。时间戳仍是 epoch 秒（DOUBLE PRECISION）。
-- 幂等：IF NOT EXISTS，可反复执行。

CREATE TABLE IF NOT EXISTS reviews (
    id         SERIAL PRIMARY KEY,
    round_no   INTEGER NOT NULL,
    attempt    INTEGER NOT NULL DEFAULT 1,
    provider   TEXT NOT NULL,
    model_ref  TEXT NOT NULL DEFAULT '',
    context_id TEXT NOT NULL,
    parse_ok   INTEGER NOT NULL DEFAULT 0,
    verdict    TEXT NOT NULL DEFAULT 'unknown',
    findings   TEXT NOT NULL DEFAULT '[]',
    created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_reviews_round ON reviews(round_no, attempt);

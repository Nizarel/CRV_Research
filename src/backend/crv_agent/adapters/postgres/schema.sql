CREATE TABLE IF NOT EXISTS crv_threads (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    search_release TEXT NOT NULL,
    kg_release TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS crv_thread_owner ON crv_threads(owner, created_at);
CREATE TABLE IF NOT EXISTS crv_runs (
    thread_id TEXT NOT NULL REFERENCES crv_threads(id),
    id TEXT NOT NULL,
    question TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed', 'cancelled')),
    result JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (thread_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS crv_one_active_turn
    ON crv_runs(thread_id) WHERE status = 'running';
CREATE TABLE IF NOT EXISTS crv_feedback (
    thread_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    rating INTEGER NOT NULL CHECK (rating IN (-1, 1)),
    comment TEXT NOT NULL,
    PRIMARY KEY (thread_id, run_id),
    FOREIGN KEY (thread_id, run_id) REFERENCES crv_runs(thread_id, id)
);

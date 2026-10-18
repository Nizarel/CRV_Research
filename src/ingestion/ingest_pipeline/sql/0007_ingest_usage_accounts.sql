-- 0007_ingest_usage_accounts: admission sums recorded usage by provider and billing account
-- (this subscription or an external one) before every paid submit. Requires 0003.

CREATE INDEX stage_attempt_usage_idx
    ON ingest.stage_attempt ((usage->>'provider'), (usage->>'account'))
    WHERE usage IS NOT NULL;

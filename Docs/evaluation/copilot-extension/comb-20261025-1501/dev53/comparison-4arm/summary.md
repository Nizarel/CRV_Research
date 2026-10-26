---
title: Stakeholder Evaluation Results
description: Execution, comparison and telemetry coverage for this evaluation run.
ms.date: 2026-10-25
---

## Summary

```json
{
  "questions": 212,
  "task_success": 8,
  "task_success_rate": 0.03773584905660377,
  "comparison_verdicts": {
    "needs_review": 35,
    "fail": 76,
    "partial": 93,
    "pass": 8
  },
  "answer_statuses": {
    "insufficient_evidence": 97,
    "answered": 91,
    "partial": 24
  },
  "execution_statuses": {
    "completed": 212
  },
  "status_vs_comparison": {
    "insufficient_evidence:needs_review": 11,
    "insufficient_evidence:fail": 62,
    "answered:partial": 56,
    "answered:pass": 6,
    "partial:needs_review": 7,
    "partial:partial": 15,
    "insufficient_evidence:partial": 22,
    "answered:fail": 12,
    "answered:needs_review": 17,
    "partial:fail": 2,
    "insufficient_evidence:pass": 2
  },
  "judge_errors": 2,
  "fact_recall_measured": 212,
  "latency_measured": 212,
  "latency_ms_p50": 54459.45960000972,
  "latency_ms_p95": 104835.66129999235,
  "tool_metrics_measured": 0,
  "token_metrics_measured": 212,
  "agent_cost_status": "unmeasured",
  "agent_cost_known_subtotals": {},
  "production_deployed": false
}
```

See [comparisons.csv](comparisons.csv) for the side-by-side review and [comparisons.jsonl](comparisons.jsonl) for lossless records.

Unmeasured mandatory checks block task success. Not-evaluated rows are not measured answer-quality failures. No production deployment was performed.

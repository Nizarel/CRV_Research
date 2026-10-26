---
title: Stakeholder Evaluation Results
description: Execution, comparison and telemetry coverage for this evaluation run.
ms.date: 2026-10-25
---

## Summary

```json
{
  "questions": 12,
  "task_success": 0,
  "task_success_rate": 0.0,
  "comparison_verdicts": {
    "partial": 11,
    "needs_review": 1
  },
  "answer_statuses": {
    "answered": 10,
    "insufficient_evidence": 2
  },
  "execution_statuses": {
    "completed": 12
  },
  "status_vs_comparison": {
    "answered:partial": 10,
    "insufficient_evidence:partial": 1,
    "insufficient_evidence:needs_review": 1
  },
  "judge_errors": 0,
  "fact_recall_measured": 12,
  "latency_measured": 12,
  "latency_ms_p50": 29260.48049999372,
  "latency_ms_p95": 43806.61939999845,
  "tool_metrics_measured": 0,
  "token_metrics_measured": 12,
  "agent_cost_status": "unmeasured",
  "agent_cost_known_subtotals": {},
  "production_deployed": false
}
```

See [comparisons.csv](comparisons.csv) for the side-by-side review and [comparisons.jsonl](comparisons.jsonl) for lossless records.

Unmeasured mandatory checks block task success. Not-evaluated rows are not measured answer-quality failures. No production deployment was performed.

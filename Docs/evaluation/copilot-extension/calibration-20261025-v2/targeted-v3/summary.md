---
title: Stakeholder Evaluation Results
description: Execution, comparison and telemetry coverage for this evaluation run.
ms.date: 2026-10-25
---

## Summary

```json
{
  "questions": 6,
  "task_success": 3,
  "task_success_rate": 0.5,
  "comparison_verdicts": {
    "pass": 3,
    "needs_review": 2,
    "fail": 1
  },
  "answer_statuses": {
    "answered": 4,
    "insufficient_evidence": 2
  },
  "execution_statuses": {
    "completed": 6
  },
  "status_vs_comparison": {
    "answered:pass": 2,
    "answered:needs_review": 2,
    "insufficient_evidence:pass": 1,
    "insufficient_evidence:fail": 1
  },
  "judge_errors": 0,
  "fact_recall_measured": 6,
  "latency_measured": 6,
  "latency_ms_p50": 48528.475649998654,
  "latency_ms_p95": 68717.48489999663,
  "tool_metrics_measured": 0,
  "token_metrics_measured": 6,
  "agent_cost_status": "unmeasured",
  "agent_cost_known_subtotals": {},
  "production_deployed": false
}
```

See [comparisons.csv](comparisons.csv) for the side-by-side review and [comparisons.jsonl](comparisons.jsonl) for lossless records.

Unmeasured mandatory checks block task success. Not-evaluated rows are not measured answer-quality failures. No production deployment was performed.

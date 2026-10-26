---
title: Stakeholder Evaluation Results
description: Execution, comparison and telemetry coverage for this evaluation run.
ms.date: 2026-10-25
---

## Summary

```json
{
  "questions": 106,
  "task_success": 3,
  "task_success_rate": 0.02830188679245283,
  "comparison_verdicts": {
    "needs_review": 17,
    "fail": 40,
    "partial": 46,
    "pass": 3
  },
  "answer_statuses": {
    "insufficient_evidence": 51,
    "answered": 32,
    "partial": 23
  },
  "execution_statuses": {
    "completed": 106
  },
  "status_vs_comparison": {
    "insufficient_evidence:needs_review": 5,
    "insufficient_evidence:fail": 33,
    "answered:partial": 20,
    "answered:pass": 2,
    "partial:needs_review": 7,
    "partial:partial": 14,
    "insufficient_evidence:partial": 12,
    "answered:fail": 5,
    "answered:needs_review": 5,
    "partial:fail": 2,
    "insufficient_evidence:pass": 1
  },
  "judge_errors": 1,
  "fact_recall_measured": 106,
  "latency_measured": 106,
  "latency_ms_p50": 61016.955049999524,
  "latency_ms_p95": 106755.31360000605,
  "tool_metrics_measured": 0,
  "token_metrics_measured": 106,
  "agent_cost_status": "unmeasured",
  "agent_cost_known_subtotals": {},
  "production_deployed": false
}
```

See [comparisons.csv](comparisons.csv) for the side-by-side review and [comparisons.jsonl](comparisons.jsonl) for lossless records.

Unmeasured mandatory checks block task success. Not-evaluated rows are not measured answer-quality failures. No production deployment was performed.

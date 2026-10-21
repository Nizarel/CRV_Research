"""Golden-set evaluation of CRV retrieval (Docs/AGENTIC_ARCHITECTURE.md §7).

``convert`` turns Docs/QA_REMINEX.xlsx Sheet2 into ``golden/crv_golden.jsonl`` (locally);
``run`` queries the private Search index (and the crv_kg read API) in the cluster, scores
anchor recall deterministically and key facts with an LLM judge, and prints the report.
"""

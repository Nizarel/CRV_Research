"""Render a bake-off report as Markdown (no I/O)."""

from __future__ import annotations

from collections import Counter

from .model import ArmScore, Report


def _fmt(value: float | int | None) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.1%}"
    return str(value)


def _usage(arm: ArmScore) -> str:
    parts = []
    for usage in arm.usage:
        tokens = usage.input_tokens + usage.output_tokens
        suffix = f", {tokens:,} tokens" if tokens else ""
        parts.append(f"{usage.provider}: {usage.pages} pages{suffix}")
    return "; ".join(parts) or "none recorded"


def render_markdown(report: Report) -> str:
    lines = [
        "# Extraction bake-off report",
        "",
        f"Generated {report.generated_at}. Gold items: {report.gold_items} "
        f"(sha256 `{report.gold_sha256[:12]}`); sample sha256 `{report.sample_sha256[:12]}`.",
        "",
        "Literal recall counts only source text. Field recall applies to CU arms. "
        "Absent checks pass when a forbidden value (e.g. a register-corrected tag) is nowhere "
        "on the page, generated text included. Tuning pages were used to adjust profiles; "
        "decide on held-out figures.",
        "",
        "| Arm | Split | Literal recall | Field recall | Absent pass | Missing evidence "
        "| Only in generated |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for arm in report.arms:
        for split in ("held_out", "tuning", "all"):
            values = arm.metrics.get(split, {})
            lines.append(
                f"| {arm.arm} | {split} | {_fmt(values.get('literal_recall'))} "
                f"| {_fmt(values.get('field_recall'))} | {_fmt(values.get('absent_pass_rate'))} "
                f"| {_fmt(values.get('missing_evidence'))} "
                f"| {_fmt(values.get('literal_only_generated'))} |"
            )
    lines += ["", "## Coverage and spend", ""]
    lines += ["| Arm | Documents found / complete | Spend | Jobs |", "|---|---|---|---|"]
    for arm in report.arms:
        jobs = ", ".join(f"{key} {value}" for key, value in sorted(arm.jobs.items())) or "n/a"
        lines.append(
            f"| {arm.arm} | {arm.documents_found}/{arm.documents} found, "
            f"{arm.documents_complete} complete | {_usage(arm)} | {jobs} |"
        )
    lines += ["", "## Failures by category", ""]
    for arm in report.arms:
        failures = Counter(
            item.category for item in arm.items if item.outcome in ("fail", "missing_evidence")
        )
        summary = ", ".join(f"{name} {count}" for name, count in sorted(failures.items()))
        lines.append(f"- **{arm.arm}**: {summary or 'none'}")
    lines += ["", "## Item outcomes", "", "| Item | Page | Check | " + " | ".join(
        arm.arm for arm in report.arms
    ) + " |", "|---|---:|---|" + "---|" * len(report.arms)]
    by_arm = {arm.arm: {item.id: item for item in arm.items} for arm in report.arms}
    first = report.arms[0].items if report.arms else ()
    for item in first:
        cells = []
        for arm in report.arms:
            result = by_arm[arm.arm].get(item.id)
            if result is None:
                cells.append("")
                continue
            where = f" ({'+'.join(result.where)})" if result.where else ""
            cells.append(result.outcome + where)
        lines.append(f"| {item.id} | {item.page} | {item.check} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"

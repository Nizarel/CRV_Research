"""Business quality is independent of status; missing telemetry is never a success."""

import asyncio
import json
from pathlib import Path

import pytest

from ingest_pipeline.tools.golden.answer_compare import (
    ComparisonVerdict,
    FactComparison,
    compare_records,
    comparison_record,
    estimated_cost,
    spreadsheet_cell,
    tool_metrics,
    usage_metrics,
    validate_verdict,
    write_report,
)
from ingest_pipeline.tools.golden.business import BusinessExpectation, BusinessQuestion, ToolRule
from ingest_pipeline.tools.golden.dataset import KeyFact


def question(tools: ToolRule | None = None) -> BusinessQuestion:
    return BusinessQuestion(
        id="Q77",
        number=77,
        question="Question",
        expected="Reference exacte",
        category="A",
        split="dev",
        key_facts=(KeyFact(text="Reference exacte"),),
        evaluation=BusinessExpectation(
            source_file="source.csv",
            source_sha256="a" * 64,
            record=1,
            line_start=2,
            line_end=2,
            tools=tools or ToolRule(),
        ),
    )


def answer(status: str = "answered") -> dict:
    return {
        "id": "Q77",
        "arm": "BASE",
        "status": status,
        "execution_status": "completed",
        "schema_valid": True,
        "schema_check_status": "valid",
        "latency_ms": 12.345,
        "answer": {"content": "Actual answer"},
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "cached_tokens": 30,
            "reasoning_tokens": 5,
        },
    }


def verdict(label: str = "pass") -> ComparisonVerdict:
    return ComparisonVerdict(
        verdict=label,
        expected_behavior_met=True,
        reference_ambiguous=False,
        issues=[],
        rationale="Comparison rationale",
        facts=[
            FactComparison(
                index=1,
                result="matched",
                expected_excerpt="Reference",
                generated_excerpt="Actual",
                explanation="Semantic match",
            )
        ],
    )


class FakeJudge:
    def __init__(self, result: ComparisonVerdict | None = None) -> None:
        self.calls = 0
        self.result = result or verdict()

    async def compare(self, question: BusinessQuestion, content: str):
        self.calls += 1
        return self.result, {"input_tokens": 10, "output_tokens": 5}


def test_success_is_not_the_agent_status_and_missing_cache_is_not_extra_tokens() -> None:
    for status in ("answered", "partial", "insufficient_evidence", "out_of_scope"):
        rows = asyncio.run(compare_records([question()], [answer(status)], FakeJudge(), repeats=1))
        assert rows[0]["task_success"] is True
        assert rows[0]["answer_status"] == status
        assert rows[0]["total_tokens"] == 120
        assert rows[0]["agent_cost_actual"] is None
        assert rows[0]["reference_fact_recall"] == 1
        assert rows[0]["matched_elements"] == ["Reference"]
        assert rows[0]["comparison_rationale"] == "Comparison rationale"
    rows = asyncio.run(
        compare_records([question()], [answer()], FakeJudge(verdict("fail")), repeats=1)
    )
    assert rows[0]["task_success"] is False


def test_complete_trace_label_without_invocations_is_still_unmeasured() -> None:
    result = tool_metrics({"tool_trace_status": "complete"}, ToolRule(all_of=("calculate",)))
    assert result["expected_tool_called"] is None
    assert result["tool_call_count"] is None


def test_unknown_tool_trace_blocks_mandatory_check_without_fabricating_false_or_zero() -> None:
    required = question(ToolRule(all_of=("calculate",)))
    rows = asyncio.run(compare_records([required], [answer()], FakeJudge(), repeats=1))
    assert rows[0]["expected_tool_called"] is None
    assert rows[0]["tool_call_count"] is None
    assert rows[0]["task_success"] is False


def test_observed_tools_deduplicate_and_distinguish_zero_from_unknown() -> None:
    call = {"id": "one", "name": "calculate", "outcome": "success"}
    row = {"tool_trace_status": "complete", "tool_invocations": [call, call]}
    result = tool_metrics(row, ToolRule(all_of=("calculate",)))
    assert result["expected_tool_called"] is True
    assert result["tool_call_count"] == 1
    result = tool_metrics(
        {"tool_trace_status": "complete", "tool_invocations": []}, ToolRule(all_of=("calculate",))
    )
    assert result["expected_tool_called"] is False
    assert result["tool_call_count"] == 0


def test_fabricated_excerpt_or_missing_index_cannot_pass() -> None:
    bad = verdict()
    bad.facts[0].generated_excerpt = "invented"
    with pytest.raises(ValueError, match="actual answer"):
        validate_verdict(question(), "Actual answer", bad)
    bad = verdict()
    bad.facts.append(bad.facts[0])
    with pytest.raises(ValueError, match="exactly once"):
        validate_verdict(question(), "Actual answer", bad)


def indexed_verdict(start_line=1, end_line=1) -> dict:
    data = verdict().model_dump(mode="json")
    data["facts"] = [{
        "index": 1, "result": "matched", "start_line": start_line,
        "end_line": end_line, "explanation": "Semantic match",
    }]
    return data


def test_indexed_quotes_preserve_original_markdown_unicode_and_line_endings() -> None:
    from ingest_pipeline.tools.golden.copilot_judge import indexed_comparison

    content = "Heading\r\n**L\u2019\u00e9chantillon** : 42\r\n- second line\n"
    result = indexed_comparison(json.dumps(indexed_verdict(2, 3)), question(), content)
    assert result.facts[0].generated_excerpt == content.removeprefix("Heading\r\n")
    assert result.facts[0].expected_excerpt == question().key_facts[0].text
    validate_verdict(question(), content, result)


@pytest.mark.parametrize(("start_line", "end_line"), [
    (0, 1), (1, 2), (2, 1), (None, 1), (1, None), (True, 1),
])
def test_indexed_quotes_reject_invalid_or_fabricated_line_ranges(start_line, end_line) -> None:
    from ingest_pipeline.tools.golden.copilot_judge import indexed_comparison

    with pytest.raises(ValueError, match=r"line range is invalid|valid integer"):
        indexed_comparison(json.dumps(indexed_verdict(start_line, end_line)), question(), "Actual")


def test_indexed_quotes_still_reject_missing_evidence_indices_and_inconsistent_passes() -> None:
    from ingest_pipeline.tools.golden.copilot_judge import indexed_comparison

    missing = indexed_verdict(None, None)
    with pytest.raises(ValueError, match="requires a generated excerpt"):
        indexed_comparison(json.dumps(missing), question(), "Actual")
    duplicate = indexed_verdict()
    duplicate["facts"] *= 2
    with pytest.raises(ValueError, match="exactly once"):
        indexed_comparison(json.dumps(duplicate), question(), "Actual")
    inconsistent = indexed_verdict()
    inconsistent["expected_behavior_met"] = False
    with pytest.raises(ValueError, match="contradicts"):
        indexed_comparison(json.dumps(inconsistent), question(), "Actual")
    invented = indexed_verdict()
    invented["facts"][0]["generated_excerpt"] = "not in the source"
    with pytest.raises(ValueError, match="extra_forbidden"):
        indexed_comparison(json.dumps(invented), question(), "Actual")


def test_failed_generation_and_missing_rows_stay_in_denominator(tmp_path: Path) -> None:
    failed = {**answer(), "execution_status": "timeout", "schema_valid": False}
    judge = FakeJudge()
    rows = asyncio.run(compare_records([question()], [failed], judge))
    assert judge.calls == 0
    assert rows[0]["comparison_verdict"] == "not_evaluated"
    assert not rows[0]["task_success"]
    rows = asyncio.run(compare_records([question()], [], judge))
    assert rows[0]["execution_status"] == "not_run"
    assert write_report(tmp_path, rows)["questions"] == 1


def test_resume_does_not_repeat_successful_judging() -> None:
    judge, attempts = FakeJudge(), []
    asyncio.run(
        compare_records([question()], [answer()], judge, emit=attempts.append, model="fixed")
    )
    assert judge.calls == 3
    rows = asyncio.run(
        compare_records([question()], [answer()], judge, cached=attempts, model="fixed")
    )
    assert judge.calls == 3
    assert rows[0]["judge_attempts"] == 3
    assert rows[0]["judge_agreement"] == 1


def test_judge_failure_is_not_silently_dropped() -> None:
    class Broken:
        async def compare(self, question, content):
            raise TimeoutError("provider down")

    rows = asyncio.run(compare_records([question()], [answer()], Broken()))
    assert rows[0]["judge_errors"] == 3
    assert rows[0]["comparison_verdict"] == "error"
    assert not rows[0]["task_success"]


def test_rejected_judge_response_retains_usage_and_validation_evidence() -> None:
    rejected = verdict()
    rejected.facts[0].generated_excerpt = "not in the actual answer"
    attempts = []
    rows = asyncio.run(compare_records(
        [question()], [answer()], FakeJudge(rejected), repeats=1, emit=attempts.append,
    ))
    assert attempts[0]["usage"]["input_tokens"] == 10
    assert attempts[0]["received_comparison"]["facts"][0]["generated_excerpt"] == (
        "not in the actual answer"
    )
    assert "actual answer" in attempts[0]["validation_error"]
    assert rows[0]["comparison_verdict"] == "error"
    assert not rows[0]["task_success"]


@pytest.mark.parametrize("status", ["error", "ok"])
def test_failed_sdk_call_or_invalid_json_retains_provider_usage(monkeypatch, status: str) -> None:
    from ingest_pipeline.tools.golden.copilot_judge import CopilotNodeSDKJudge

    judge = CopilotNodeSDKJudge("fixed")
    attempts = []

    async def send(message):
        judge.pending[message["id"]].set_result({
            "status": status, "error_type": "SDKError", "content": "not JSON",
            "usage": {"events": [{"type": "assistant.usage", "data": {"inputTokens": 12}}]},
        })

    monkeypatch.setattr(judge, "_send", send)
    rows = asyncio.run(compare_records(
        [question()], [answer()], judge, repeats=1, emit=attempts.append,
    ))
    assert attempts[0]["usage"]["events"][0]["data"]["inputTokens"] == 12
    if status == "ok":
        assert attempts[0]["received_content"] == "not JSON"
        assert "Invalid JSON" in attempts[0]["validation_error"]
    assert rows[0]["judge_errors"] == 1
    assert not rows[0]["task_success"]


def test_missing_usage_is_unmeasured_and_reports_preserve_full_text(tmp_path: Path) -> None:
    assert usage_metrics({})["total_tokens"] is None
    row = comparison_record(question(), answer(), [])
    write_report(tmp_path, [row])
    restored = json.loads((tmp_path / "comparisons.jsonl").read_text())
    assert restored["expected_answer"] == "Reference exacte"
    assert restored["generated_answer"] == "Actual answer"
    assert spreadsheet_cell('=HYPERLINK("bad")').startswith("'")


def test_duplicate_or_unknown_answers_fail_before_judge_calls() -> None:
    judge = FakeJudge()
    with pytest.raises(ValueError, match="duplicate"):
        asyncio.run(compare_records([question()], [answer(), answer()], judge))
    assert judge.calls == 0


def test_copilot_session_is_isolated_and_prompt_preserves_reference() -> None:
    from ingest_pipeline.tools.golden.copilot_judge import session_options

    options = session_options("explicit-model", lambda *_: None)
    assert options["available_tools"] == []
    assert options["enable_config_discovery"] is False
    assert options["skip_custom_instructions"] is True
    assert options["enable_session_store"] is False
    assert options["enable_skills"] is False
    assert options["memory"] == {"enabled": False}


def test_business_cli_import_and_loader_preserve_metadata(tmp_path: Path) -> None:
    from ingest_pipeline.tools.golden.__main__ import load_golden, main
    from tests.unit.test_golden_business import source

    original = source(tmp_path, [("Q", "Exact reference")])
    output, review = tmp_path / "golden.jsonl", tmp_path / "review.csv"
    assert (
        main(
            [
                "import-business",
                "--csv",
                str(original),
                "--out",
                str(output),
                "--review",
                str(review),
            ]
        )
        == 0
    )
    [loaded] = load_golden(str(output))
    assert isinstance(loaded, BusinessQuestion)
    assert loaded.expected == "Exact reference"


def test_judge_receives_frozen_reference_issues_without_assistant_review_labels() -> None:
    from ingest_pipeline.tools.golden.copilot_judge import judge_payload

    original = question()
    annotated = original.model_copy(update={
        "evaluation": original.evaluation.model_copy(update={
            "review_notes": ("Reference contains an unresolved difference/ratio conflict",),
        }),
    })
    payload = judge_payload(annotated, "Actual answer")
    assert payload["expected_answer"] == original.expected
    assert payload["reference_elements"][0]["text"] == original.key_facts[0].text
    assert payload["reference_review"] == {
        "status": "proposed",
        "notes": ["Reference contains an unresolved difference/ratio conflict"],
    }
    assert payload["generated_answer"] == "Actual answer"
    assert "expected_verdict" not in payload
    assert "answer_status" not in payload


def test_versioned_cost_uses_provider_usage_and_does_not_double_bill_cache() -> None:
    book = {
        "currency": "USD",
        "effective_date": "2026-10-24",
        "source_url": "https://pricing.test",
        "models": {
            "served": {
                "input_per_million": "10",
                "output_per_million": "20",
                "cached_input_per_million": "1",
            }
        },
    }
    usage = {
        "id": "call1",
        "model": "served",
        "usage_source": "provider",
        "input_tokens": 100,
        "output_tokens": 20,
        "cached_input_tokens": 30,
    }
    row = {"model_usage": [usage, usage], "model_usage_status": "complete"}
    result = estimated_cost(row, book)
    assert result["agent_cost_estimated"] == "0.00113"
    assert result["agent_cost_status"] == "estimated"
    assert (
        estimated_cost({**row, "model_usage_status": "partial"}, book)["agent_cost_estimated"]
        is None
    )
    assert estimated_cost(row, None) == {}
    with pytest.raises(ValueError, match="cache subset"):
        estimated_cost({**row, "model_usage": [{**usage, "cached_input_tokens": 101}]}, book)


def test_cli_json_output_is_validated_without_markdown_or_fabricated_usage() -> None:
    from ingest_pipeline.tools.golden.copilot_judge import parse_cli_output

    payload = json.dumps(
        {"type": "assistant.message", "data": {"content": verdict().model_dump_json()}}
    )
    result, usage = parse_cli_output(payload)
    assert result.verdict == "pass"
    assert usage == {"events": []}
    wrapped = json.dumps(
        {
            "type": "assistant.message",
            "data": {
                "content": "```json\n" + verdict().model_dump_json() + "\n```",
            },
        }
    )
    assert parse_cli_output(wrapped)[0].verdict == "pass"
    with pytest.raises(ValueError, match="no final"):
        parse_cli_output(json.dumps({"type": "session.start"}))


def test_cli_has_no_tools_and_kills_timed_out_process(monkeypatch) -> None:
    import shutil

    from ingest_pipeline.tools.golden.copilot_judge import CopilotCLIJudge, cli_command

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/copilot")
    command = cli_command("fixed", "prompt")
    assert "--available-tools=" in command
    assert "--disable-builtin-mcps" in command
    assert "--no-custom-instructions" in command
    assert "--allow-all-tools" not in command

    class Process:
        returncode = None
        killed = False
        reaped = False

        async def communicate(self):
            raise TimeoutError("expired")

        def kill(self):
            self.killed = True

        async def wait(self):
            self.reaped = True

    process = Process()

    async def spawn(*args, **kwargs):
        assert kwargs["stdin"] == asyncio.subprocess.DEVNULL
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(TimeoutError):
        asyncio.run(CopilotCLIJudge("fixed").compare(question(), "Actual answer"))
    assert process.killed
    assert process.reaped


@pytest.mark.live
@pytest.mark.asyncio
async def test_official_node_copilot_sdk_can_grade_a_synthetic_reference() -> None:
    import os

    from ingest_pipeline.tools.golden.copilot_judge import CopilotNodeSDKJudge

    if os.environ.get("CRV_CONFIRM_COPILOT_LIVE") != "1":
        pytest.skip("Explicit opt-in required for paid Copilot SDK calls")
    probe = question().model_copy(update={
        "question": "Quelle est la valeur?", "expected": "42",
        "key_facts": (KeyFact(text="42"),),
    })
    async with CopilotNodeSDKJudge("claude-haiku-5.5", timeout=90) as judge:
        result, usage = await judge.compare(probe, "La valeur est 42.")
        validate_verdict(probe, "La valeur est 42.", result)
    assert result.verdict == "pass"
    assert usage["provider"] == "github-copilot-sdk"
    assert usage["sdk_version"] == "1.0.26"

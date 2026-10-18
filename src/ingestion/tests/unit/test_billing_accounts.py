"""Billing accounts: list-price cost, settings validation and account choice."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from evidence_fakes import FakeContentAnalysis
from fixture_builders import repo_file
from pydantic import ValidationError

from ingest_pipeline.adapters.azure_ai.cu_registry import load_registry, verify_registry
from ingest_pipeline.config import (
    BillingSettings,
    ExternalAccount,
    PipelineSettings,
    load_billing,
)
from ingest_pipeline.domain.cu import PROVIDER as CU
from ingest_pipeline.domain.cu import usage_from
from ingest_pipeline.domain.errors import ConfigurationError
from ingest_pipeline.domain.models import ProviderUsage
from ingest_pipeline.domain.spend import Prices
from ingest_pipeline.domain.states import StageName
from ingest_pipeline.ports.ledger import ActiveWork
from ingest_pipeline.runtime.bootstrap import _matching_analyzers, spend_policy
from ingest_pipeline.stages.spend import BillingAccount, SpendPolicy

PRICES = Prices(
    di_page_usd=0.01,
    cu_page_estimate_usd=0.05,
    cu_meter_usd={
        "documentPagesStandard": 0.005,
        "contextualizationTokens": 0.000001,
        "gpt-5.4-mini-input": 0.000000825,
        "gpt-5.4-mini-cached-input": 0.0000000825,
        "gpt-5.4-mini-output": 0.00000495,
    },
)
PRICED = {
    "di_page_usd": 0.01,
    "cu_page_estimate_usd": 0.05,
    "cu_meter_usd": {"documentPagesStandard": 0.005},
}
REGISTRY = "infra/content-understanding/ingestion-pilot.json"


def test_cu_cost_prices_every_reported_meter() -> None:
    usage = usage_from(
        {
            "documentPagesStandard": 1,
            "contextualizationTokens": 1000,
            "tokens": {
                "gpt-5.4-mini-input": 16278,
                "gpt-5.4-mini-output": 2085,
                "gpt-5.4-mini-cached-input": 4608,
            },
        },
        requests=2,
    )
    cost, unpriced = PRICES.cu_cost(usage)
    expected = 0.005 + 0.001 + 16278 * 0.000000825 + 2085 * 0.00000495 + 4608 * 0.0000000825
    assert cost == pytest.approx(expected)
    assert unpriced == []
    assert PRICES.di_cost(3) == pytest.approx(0.03)


def test_unpriced_meters_are_listed_and_never_cost_less_than_the_page_estimate() -> None:
    usage = usage_from(
        {"documentPagesStandard": 2, "tokens": {"other-model-input": 10}}, requests=1
    )
    cost, unpriced = PRICES.cu_cost(usage)
    assert unpriced == ["other-model-input"]
    assert cost == pytest.approx(2 * 0.05)


def test_estimated_usage_without_meters_costs_the_page_estimate() -> None:
    usage = ProviderUsage(provider=CU, pages=3, details={"failed_analysis": True})
    assert PRICES.cu_cost(usage) == (pytest.approx(0.15), [])


def _account(**fields: object) -> dict[str, object]:
    return {
        "name": "partner",
        "cu_endpoint": "https://partner.services.ai.azure.com",
        "key_file": "/var/run/secrets/tizert/accounts/partner",
        **fields,
    }


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({"accounts": [_account()]}, "only used once budget_usd"),
        ({"budget_usd": 600}, "need di_page_usd"),
        ({"budget_usd": 600, **PRICED, "accounts": [_account(), _account()]}, "unique"),
        (
            {"budget_usd": 600, **PRICED, "accounts": [_account(cu_endpoint=None)]},
            "needs a DI or CU endpoint",
        ),
        ({"budget_usd": 600, **PRICED, "accounts": [_account(name="Partner")]}, "pattern"),
    ],
)
def test_billing_settings_reject_inconsistent_accounts(
    document: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        BillingSettings.model_validate(document)


def test_billing_file_maps_to_an_ordered_spend_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "billing.json"
    path.write_text(
        json.dumps(
            {
                "budget_usd": 580,
                **PRICED,
                "accounts": [_account(budget_usd=900, cu_max_inflight=8)],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TIZ_BILLING_PATH", str(path))
    billing = load_billing(PipelineSettings().billing_path)
    expected = ExternalAccount.model_validate(_account(budget_usd=900, cu_max_inflight=8))
    assert billing.accounts == (expected,)
    policy = spend_policy(billing)
    assert policy.accounts == (
        BillingAccount(None, 580),
        BillingAccount("partner", 900, cu_max_inflight=8),
    )
    assert policy.prices == Prices(0.01, 0.05, {"documentPagesStandard": 0.005})
    assert spend_policy(load_billing(None)) == SpendPolicy()


@dataclass
class _Workload:
    spent: dict[str | None, float] = field(default_factory=dict)
    in_flight: dict[tuple[StageName, str | None], int] = field(default_factory=dict)

    async def account_spend(self, account: str | None, providers: object) -> float:
        return self.spent.get(account, 0.0)

    async def account_work(self, stage: StageName, account: str | None) -> ActiveWork:
        return ActiveWork(billable_pages=self.in_flight.get((stage, account), 0))


BUDGETED = SpendPolicy(
    accounts=(BillingAccount(budget_usd=1.0), BillingAccount("partner", 2.0)), prices=PRICES
)


def _cu_page(prices: Prices) -> float:
    return prices.cu_page_estimate_usd


async def _choose(policy: SpendPolicy, workload: _Workload, *, pages: int = 1, serves=None):
    return await policy.candidates(
        workload,  # type: ignore[arg-type]
        provider=CU,
        pages=pages,
        page_usd=_cu_page,
        serves=serves or (lambda account: True),
    )


def _names(choices) -> list[str | None]:
    return [account.name for account in choices.accounts]


async def test_without_budgets_the_first_serving_account_takes_the_work() -> None:
    policy = SpendPolicy(accounts=(BillingAccount(), BillingAccount("partner")))
    assert _names(await _choose(policy, _Workload())) == [None]
    only_partner = await _choose(policy, _Workload(), serves=lambda account: account is not None)
    assert _names(only_partner) == ["partner"]
    with pytest.raises(ConfigurationError):
        await _choose(policy, _Workload(), serves=lambda account: False)


async def test_accounts_fill_in_order_counting_spent_and_in_flight_work() -> None:
    assert _names(await _choose(BUDGETED, _Workload(spent={None: 0.9}))) == [None]
    over = _Workload(spent={None: 0.9}, in_flight={(StageName.DI_POLL, None): 6})
    assert _names(await _choose(BUDGETED, over)) == ["partner"]
    assert _names(await _choose(BUDGETED, _Workload(spent={None: 0.96}))) == ["partner"]


async def test_accounts_with_their_own_slots_run_in_parallel_within_budget() -> None:
    parallel = SpendPolicy(
        accounts=(
            BillingAccount(budget_usd=1.0),
            BillingAccount("partner", 2.0, cu_max_inflight=4),
            BillingAccount("overflow", 2.0),
        ),
        prices=PRICES,
    )
    assert _names(await _choose(parallel, _Workload())) == [None, "partner"]
    assert _names(await _choose(parallel, _Workload(spent={"partner": 2.0}))) == [None]
    assert _names(await _choose(parallel, _Workload(spent={None: 1.0}))) == ["partner"]
    assert parallel.accounts[1].max_inflight("document_intelligence") is None


async def test_every_budget_committed_holds_the_work() -> None:
    workload = _Workload(
        spent={None: 0.99, "partner": 1.9},
        in_flight={(StageName.CU_POLL, "partner"): 2},
    )
    choice = await _choose(BUDGETED, workload)
    assert choice.accounts == ()
    assert choice.blocked == (
        "Account budgets reached: this subscription 0.99 of 1.00 USD; partner 2.00 of 2.00 USD"
    )
    skipped = await _choose(BUDGETED, _Workload(spent={None: 1.0}), serves=lambda a: a is None)
    assert skipped.blocked == "Account budgets reached: this subscription 1.00 of 1.00 USD"


async def test_an_account_without_a_budget_takes_whatever_is_left() -> None:
    policy = SpendPolicy(
        accounts=(BillingAccount(budget_usd=1.0), BillingAccount("partner")), prices=PRICES
    )
    assert _names(await _choose(policy, _Workload(spent={None: 5.0}))) == ["partner"]


def test_budgets_without_prices_are_rejected() -> None:
    with pytest.raises(ConfigurationError):
        SpendPolicy(accounts=(BillingAccount(budget_usd=1.0),))


async def test_only_analyzers_matching_this_subscription_get_external_work() -> None:
    path = repo_file(REGISTRY)
    document = json.loads(path.read_text(encoding="utf-8"))
    analyzers = {
        name: {**spec, "analyzerId": name, "status": "ready"}
        for name, spec in document["analyzers"].items()
    }
    defaults = {"modelDeployments": dict(document["modelDeployments"])}
    here = await verify_registry(
        FakeContentAnalysis(analyzers=analyzers, default_settings=defaults),
        await load_registry(path),
    )
    drifted = {name: dict(spec) for name, spec in analyzers.items()}
    drifted["tizert_pfd_sheet_v1"]["description"] = "edited in the portal"
    del drifted["tizert_classify_v1"]
    partner = FakeContentAnalysis(analyzers=drifted, default_settings=defaults)
    settings = PipelineSettings(cu={"registry_path": path})
    account = ExternalAccount.model_validate(_account())

    matching = await _matching_analyzers(settings, account, partner, here.catalog)

    assert matching == frozenset(here.catalog.profiles) - {
        "tizert_pfd_sheet_v1",
        "tizert_classify_v1",
    }
    assert "tizert_pfd_sheet_v2" in matching

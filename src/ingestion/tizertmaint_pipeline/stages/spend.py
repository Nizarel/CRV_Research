"""Which billing account runs the next paid analysis.

Accounts are filled in order: this subscription's resources first, then each external account.
An account is skipped once its spent plus in-flight cost would pass its USD budget; when every
account is at its budget the submit waits. Concurrent submits can each pass the same check, so
an account may end up to one page estimate per in-flight slot over its budget.

An external account with its own in-flight limit for a provider also runs in parallel: when the
accounts before it are busy, it takes the work into its own slots instead of waiting.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..domain import cu
from ..domain.errors import ConfigurationError
from ..domain.spend import Prices
from ..domain.states import StageName
from ..ports.ledger import WorkloadReader
from .base import StageContext

DI_PROVIDER = "document_intelligence"
PAID_PROVIDERS = (DI_PROVIDER, cu.PROVIDER)


@dataclass(frozen=True)
class BillingAccount:
    name: str | None = None
    """``None`` is this subscription's resources, reached with the workload identity."""
    budget_usd: float | None = None
    """``None``: no budget of its own; the provider page and token caps still apply."""
    cu_max_inflight: int | None = None
    """Own CU slots, used in parallel with earlier accounts; ``None``: overflow only."""
    di_max_inflight: int | None = None

    @property
    def label(self) -> str:
        return self.name or "this subscription"

    def max_inflight(self, provider: str) -> int | None:
        return self.di_max_inflight if provider == DI_PROVIDER else self.cu_max_inflight


@dataclass(frozen=True)
class AccountChoices:
    accounts: tuple[BillingAccount, ...] = ()
    """Accounts to try for a remote slot, in order."""
    blocked: str | None = None


async def start_on_account(
    context: StageContext,
    accounts: tuple[BillingAccount, ...],
    *,
    provider: str,
    polls: StageName,
    default_limit: int,
) -> tuple[bool, str | None]:
    """Admit the leased submit into the first account with a free slot: ``(admitted, account)``.

    This subscription and overflow-only accounts use ``default_limit``."""

    workload = context.services.require_workload()
    now = context.services.clock.now()
    for account in accounts:
        if await workload.start_remote(
            context.lease,
            polls=polls,
            limit=account.max_inflight(provider) or default_limit,
            account=account.name,
            now=now,
        ):
            return True, account.name
    return False, None


@dataclass(frozen=True)
class SpendPolicy:
    accounts: tuple[BillingAccount, ...] = (BillingAccount(),)
    prices: Prices | None = None

    def __post_init__(self) -> None:
        if self.budgeted and self.prices is None:
            raise ConfigurationError("USD budgets need prices")

    @property
    def budgeted(self) -> bool:
        return any(account.budget_usd is not None for account in self.accounts)

    async def candidates(
        self,
        workload: WorkloadReader,
        *,
        provider: str,
        pages: int,
        page_usd: Callable[[Prices], float],
        serves: Callable[[str | None], bool],
    ) -> AccountChoices:
        """The first account with budget for ``pages`` more, then every later account with budget
        and its own ``provider`` slots (parallel accounts)."""

        serving = [account for account in self.accounts if serves(account.name)]
        if not serving:
            raise ConfigurationError("No billing account serves this provider in this worker")
        chosen: list[BillingAccount] = []
        notes: list[str] = []
        for account in serving:
            if chosen and account.max_inflight(provider) is None:
                continue
            note = await self._over_budget(workload, account, pages, page_usd)
            if note is None:
                chosen.append(account)
            elif not chosen:
                notes.append(note)
        if not chosen:
            return AccountChoices(blocked="Account budgets reached: " + "; ".join(notes))
        return AccountChoices(tuple(chosen))

    async def _over_budget(
        self,
        workload: WorkloadReader,
        account: BillingAccount,
        pages: int,
        page_usd: Callable[[Prices], float],
    ) -> str | None:
        if account.budget_usd is None or self.prices is None:
            return None
        prices = self.prices
        committed = await workload.account_spend(account.name, PAID_PROVIDERS)
        for stage, price in (
            (StageName.DI_POLL, prices.di_page_usd),
            (StageName.CU_POLL, prices.cu_page_estimate_usd),
        ):
            work = await workload.account_work(stage, account.name)
            committed += work.billable_pages * price
        if committed + pages * page_usd(prices) <= account.budget_usd:
            return None
        return f"{account.label} {committed:.2f} of {account.budget_usd:.2f} USD"

from typing import Protocol, runtime_checkable

from app.models.commerce import PurchaseOption


@runtime_checkable
class BaseCommerceProvider(Protocol):
    """Abstract interface protocol for external commerce comparison providers.

    Invariants:
    - [INVARIANT-A2-1: ADAPTER_TRANSPARENCY] Callers interact only with BaseCommerceProvider;
      concrete adapters encapsulate provider-specific auth, queries, and normalization.
    - [INVARIANT-A2-2: ZERO_PRICE_PERSISTENCE] Output PurchaseOption objects contain ephemeral
      price display values and must never mutate persistent inventory or shopping lists.
    - [INVARIANT-A2-4: FAULT_TOLERANT_DEGRADATION] search_options must handle upstream errors
      gracefully without throwing unhandled 500 exceptions to caller.
    """

    async def search_options(
        self,
        keyword: str,
        limit: int = 5,
    ) -> list[PurchaseOption]:
        """Search products and return normalized PurchaseOption candidates.

        Args:
            keyword: Product title or category keyword string.
            limit: Maximum candidate purchase options to return (default: 5).

        Returns:
            List of normalized PurchaseOption objects. Returns empty list if
            keyword is empty or no matches found.
        """
        ...

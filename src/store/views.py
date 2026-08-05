"""Per-agent scoped data access.

Every agent receives a *view*, not the store. A view exposes only the tables its
owner is allowed to touch, so "quyen truy cap" is a real runtime property we can
point at in architecture.md instead of a claim in a diagram. The bus builds the
view from the agent's declared ``capabilities`` and refuses unknown scopes.
"""

from __future__ import annotations

from dataclasses import dataclass

from .olist_store import Customer, OlistStore, Order, OrderItem, Payment


class ScopeError(PermissionError):
    """Raised when an agent reaches for a table it never declared."""


# Capability name -> human description (also rendered into architecture.md).
SCOPES: dict[str, str] = {
    "orders": "olist_orders_dataset.csv",
    "customers": "olist_customers_dataset.csv (+ customer_unique_id fan-out)",
    "items": "olist_order_items_dataset.csv",
    "payments": "olist_order_payments_dataset.csv",
    "products": "olist_products_dataset.csv + category translation",
    "sellers": "olist_sellers_dataset.csv (existence checks only)",
}


@dataclass
class DataView:
    """A capability-limited handle onto the store."""

    _store: OlistStore
    _scopes: frozenset[str]
    owner: str

    def _guard(self, scope: str) -> None:
        if scope not in self._scopes:
            raise ScopeError(
                f"agent '{self.owner}' has no '{scope}' capability "
                f"(granted: {sorted(self._scopes)})"
            )

    # -- orders ------------------------------------------------------------
    def order(self, order_id: str) -> Order | None:
        self._guard("orders")
        return self._store.order(order_id)

    # -- customers ---------------------------------------------------------
    def customer(self, customer_id: str) -> Customer | None:
        self._guard("customers")
        return self._store.customer(customer_id)

    def sibling_orders(self, customer_unique_id: str, exclude: str) -> list[str]:
        self._guard("customers")
        return self._store.sibling_orders(customer_unique_id, exclude)

    # -- items -------------------------------------------------------------
    def items(self, order_id: str) -> list[OrderItem]:
        self._guard("items")
        return self._store.items(order_id)

    # -- payments ----------------------------------------------------------
    def payments(self, order_id: str) -> list[Payment]:
        self._guard("payments")
        return self._store.payments(order_id)

    # -- products ----------------------------------------------------------
    def category_of(self, product_id: str) -> str:
        self._guard("products")
        return self._store.product_category.get(product_id, "")

    def product_exists(self, product_id: str) -> bool:
        self._guard("products")
        return self._store.product_exists(product_id)

    # -- sellers -----------------------------------------------------------
    def seller_exists(self, seller_id: str) -> bool:
        self._guard("sellers")
        return self._store.seller_exists(seller_id)


def build_view(store: OlistStore, owner: str, scopes: frozenset[str]) -> DataView:
    unknown = scopes - SCOPES.keys()
    if unknown:
        raise ScopeError(f"agent '{owner}' declares unknown scopes: {sorted(unknown)}")
    return DataView(_store=store, _scopes=scopes, owner=owner)

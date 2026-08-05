"""Olist CSV store: the single source of ground truth for the whole system.

Design rules enforced here:
  * Loaded once per run, indexed in memory, read-only afterwards.
  * Row order from the CSV is preserved (``_row`` index) because the output
    schema requires "thu tu on dinh theo du lieu nguon".
  * Timestamps are kept BOTH as the raw CSV string (what we emit) and as a
    parsed datetime (what we compute with). We never reformat a timestamp.
  * geolocation and order_reviews are deliberately NOT loaded: no field in the
    output schema derives from them, and geolocation alone is 62 MB.
"""

from __future__ import annotations

import csv
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def parse_ts(raw: str | None) -> datetime | None:
    """CSV timestamp -> datetime, or None when the cell is blank."""
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, TS_FORMAT)
    except ValueError:
        return None


def clean_ts(raw: str | None) -> str | None:
    """CSV timestamp -> the exact string we emit, or None when blank."""
    if not raw:
        return None
    raw = raw.strip()
    return raw or None


@dataclass(frozen=True)
class Order:
    order_id: str
    customer_id: str
    order_status: str
    purchase_at: str | None
    approved_at: str | None
    carrier_handoff_at: str | None
    delivered_at: str | None
    estimated_delivery_at: str | None
    row: int

    @property
    def delivered_dt(self) -> datetime | None:
        return parse_ts(self.delivered_at)

    @property
    def estimated_dt(self) -> datetime | None:
        return parse_ts(self.estimated_delivery_at)

    @property
    def carrier_handoff_dt(self) -> datetime | None:
        return parse_ts(self.carrier_handoff_at)


@dataclass(frozen=True)
class OrderItem:
    order_id: str
    order_item_id: int
    product_id: str
    seller_id: str
    shipping_limit_at: str | None
    price: float
    freight_value: float

    @property
    def item_ref(self) -> str:
        return f"{self.order_id}:{self.order_item_id}"

    @property
    def shipping_limit_dt(self) -> datetime | None:
        return parse_ts(self.shipping_limit_at)


@dataclass(frozen=True)
class Payment:
    order_id: str
    payment_sequential: int
    payment_type: str
    payment_installments: int
    payment_value: float

    @property
    def payment_ref(self) -> str:
        return f"{self.order_id}:{self.payment_sequential}"


@dataclass(frozen=True)
class Customer:
    customer_id: str
    customer_unique_id: str
    zip_prefix: str
    city: str
    state: str


@dataclass
class OlistStore:
    orders: dict[str, Order] = field(default_factory=dict)
    items_by_order: dict[str, list[OrderItem]] = field(default_factory=dict)
    payments_by_order: dict[str, list[Payment]] = field(default_factory=dict)
    customers: dict[str, Customer] = field(default_factory=dict)
    orders_by_unique_customer: dict[str, list[str]] = field(default_factory=dict)
    product_category: dict[str, str] = field(default_factory=dict)
    seller_ids: set[str] = field(default_factory=set)
    category_translation: dict[str, str] = field(default_factory=dict)

    # -- lookups -----------------------------------------------------------
    def order(self, order_id: str) -> Order | None:
        return self.orders.get(order_id)

    def items(self, order_id: str) -> list[OrderItem]:
        return self.items_by_order.get(order_id, [])

    def payments(self, order_id: str) -> list[Payment]:
        return self.payments_by_order.get(order_id, [])

    def customer(self, customer_id: str) -> Customer | None:
        return self.customers.get(customer_id)

    def sibling_orders(self, customer_unique_id: str, exclude: str) -> list[str]:
        """Other orders of the same human, in orders.csv row order."""
        return [
            oid
            for oid in self.orders_by_unique_customer.get(customer_unique_id, [])
            if oid != exclude
        ]

    def product_exists(self, product_id: str) -> bool:
        return product_id in self.product_category

    def seller_exists(self, seller_id: str) -> bool:
        return seller_id in self.seller_ids

    @property
    def stats(self) -> dict[str, int]:
        return {
            "orders": len(self.orders),
            "orders_with_items": len(self.items_by_order),
            "orders_with_payments": len(self.payments_by_order),
            "customers": len(self.customers),
            "products": len(self.product_category),
            "sellers": len(self.seller_ids),
        }


def _read(path: Path):
    # utf-8-sig: product_category_name_translation.csv ships with a BOM.
    with path.open(encoding="utf-8-sig", newline="") as fh:
        yield from csv.DictReader(fh)


def load_store(data_dir: Path) -> OlistStore:
    store = OlistStore()

    for row_idx, row in enumerate(_read(data_dir / "olist_orders_dataset.csv")):
        order = Order(
            order_id=row["order_id"],
            customer_id=row["customer_id"],
            order_status=row["order_status"].strip(),
            purchase_at=clean_ts(row["order_purchase_timestamp"]),
            approved_at=clean_ts(row["order_approved_at"]),
            carrier_handoff_at=clean_ts(row["order_delivered_carrier_date"]),
            delivered_at=clean_ts(row["order_delivered_customer_date"]),
            estimated_delivery_at=clean_ts(row["order_estimated_delivery_date"]),
            row=row_idx,
        )
        store.orders[order.order_id] = order

    for row in _read(data_dir / "olist_customers_dataset.csv"):
        store.customers[row["customer_id"]] = Customer(
            customer_id=row["customer_id"],
            customer_unique_id=row["customer_unique_id"],
            zip_prefix=row["customer_zip_code_prefix"],
            city=row["customer_city"],
            state=row["customer_state"],
        )

    # customer_unique_id -> [order_id...] in orders.csv row order.
    by_unique: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for order in store.orders.values():
        cust = store.customers.get(order.customer_id)
        if cust is None:
            continue
        by_unique[cust.customer_unique_id].append((order.row, order.order_id))
    store.orders_by_unique_customer = {
        uid: [oid for _, oid in sorted(pairs)] for uid, pairs in by_unique.items()
    }

    items: dict[str, list[OrderItem]] = defaultdict(list)
    for row in _read(data_dir / "olist_order_items_dataset.csv"):
        items[row["order_id"]].append(
            OrderItem(
                order_id=row["order_id"],
                order_item_id=int(row["order_item_id"]),
                product_id=row["product_id"],
                seller_id=row["seller_id"],
                shipping_limit_at=clean_ts(row["shipping_limit_date"]),
                price=float(row["price"]),
                freight_value=float(row["freight_value"]),
            )
        )
    # order_item_id is the natural, stable ordering inside an order.
    store.items_by_order = {
        oid: sorted(rows, key=lambda i: i.order_item_id) for oid, rows in items.items()
    }

    payments: dict[str, list[Payment]] = defaultdict(list)
    for row in _read(data_dir / "olist_order_payments_dataset.csv"):
        payments[row["order_id"]].append(
            Payment(
                order_id=row["order_id"],
                payment_sequential=int(row["payment_sequential"]),
                payment_type=row["payment_type"].strip(),
                payment_installments=int(row["payment_installments"]),
                payment_value=float(row["payment_value"]),
            )
        )
    store.payments_by_order = {
        oid: sorted(rows, key=lambda p: p.payment_sequential)
        for oid, rows in payments.items()
    }

    for row in _read(data_dir / "olist_products_dataset.csv"):
        store.product_category[row["product_id"]] = row["product_category_name"].strip()

    for row in _read(data_dir / "olist_sellers_dataset.csv"):
        store.seller_ids.add(row["seller_id"])

    for row in _read(data_dir / "product_category_name_translation.csv"):
        store.category_translation[row["product_category_name"]] = row[
            "product_category_name_english"
        ]

    return store

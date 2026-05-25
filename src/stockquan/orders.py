from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from stockquan.models import OrderRequest, OrderSide, OrderStatus, TradeSignal
from stockquan.storage import JsonStore


class OrderBook:
    def __init__(self, path: Path):
        self.store = JsonStore(path)

    def list_orders(self) -> list[OrderRequest]:
        raw_orders = self.store.read([])
        return [self._order_from_dict(raw) for raw in raw_orders]

    def create_from_signal(self, signal: TradeSignal) -> OrderRequest:
        order = OrderRequest(
            symbol=signal.symbol,
            side=signal.side,
            quantity=signal.quantity,
            estimated_price=signal.price,
            reason=signal.reason,
        )
        orders = self.list_orders()
        orders.append(order)
        self._save(orders)
        return order

    def approve(self, order_id: str) -> OrderRequest:
        orders = self.list_orders()
        order = self._find(orders, order_id)
        order.status = OrderStatus.APPROVED
        order.approved_at = datetime.now(timezone.utc).isoformat()
        self._save(orders)
        return order

    def reject(self, order_id: str) -> OrderRequest:
        orders = self.list_orders()
        order = self._find(orders, order_id)
        order.status = OrderStatus.REJECTED
        self._save(orders)
        return order

    def update(self, updated_order: OrderRequest) -> None:
        orders = self.list_orders()
        for index, order in enumerate(orders):
            if order.id == updated_order.id:
                orders[index] = updated_order
                self._save(orders)
                return
        raise ValueError(f"Order {updated_order.id} not found.")

    def _save(self, orders: list[OrderRequest]) -> None:
        self.store.write([asdict(order) for order in orders])

    def _find(self, orders: list[OrderRequest], order_id: str) -> OrderRequest:
        for order in orders:
            if order.id == order_id:
                return order
        raise ValueError(f"Order {order_id} not found.")

    def _order_from_dict(self, value: dict) -> OrderRequest:
        value = dict(value)
        value["side"] = OrderSide(value["side"])
        value["status"] = OrderStatus(value["status"])
        return OrderRequest(**value)

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

from stockquan.config import Settings
from stockquan.models import OrderRequest, OrderSide, OrderStatus
from stockquan.storage import JsonStore


class Broker(ABC):
    @abstractmethod
    def execute(self, order: OrderRequest) -> OrderRequest:
        raise NotImplementedError


class PaperBroker(Broker):
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = JsonStore(Path(settings.data_dir) / "paper_account.json")
        self.account = self.store.read({"cash": settings.default_cash, "positions": {}, "fills": []})

    @property
    def cash(self) -> float:
        return float(self.account["cash"])

    @property
    def positions(self) -> dict[str, int]:
        return {symbol: int(quantity) for symbol, quantity in self.account.get("positions", {}).items()}

    def execute(self, order: OrderRequest) -> OrderRequest:
        if order.status != OrderStatus.APPROVED:
            order.status = OrderStatus.FAILED
            order.message = "Order must be approved before execution."
            return order

        notional = order.estimated_notional
        if notional > self.settings.max_order_notional:
            order.status = OrderStatus.FAILED
            order.message = f"Order notional {notional:.2f} exceeds MAX_ORDER_NOTIONAL."
            return order

        positions = self.account.setdefault("positions", {})
        current_position = int(positions.get(order.symbol, 0))

        if order.side == OrderSide.BUY:
            if self.cash < notional:
                order.status = OrderStatus.FAILED
                order.message = "Insufficient paper cash."
                return order
            self.account["cash"] = round(self.cash - notional, 2)
            positions[order.symbol] = current_position + order.quantity
        else:
            if current_position < order.quantity:
                order.status = OrderStatus.FAILED
                order.message = "Insufficient paper position."
                return order
            self.account["cash"] = round(self.cash + notional, 2)
            positions[order.symbol] = current_position - order.quantity

        order.status = OrderStatus.FILLED
        order.filled_at = datetime.now(timezone.utc).isoformat()
        order.message = "Filled by paper broker."
        self.account.setdefault("fills", []).append(order)
        self.store.write(self.account)
        return order


class LiveBrokerPlaceholder(Broker):
    def execute(self, order: OrderRequest) -> OrderRequest:
        order.status = OrderStatus.FAILED
        order.message = "Live trading adapter is not configured."
        return order


def build_broker(settings: Settings) -> Broker:
    if settings.trading_mode == "paper":
        return PaperBroker(settings)
    return LiveBrokerPlaceholder()


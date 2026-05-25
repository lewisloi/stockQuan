from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    FILLED = "FILLED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class TradeSignal:
    symbol: str
    side: OrderSide
    confidence: float
    reason: str
    price: float
    quantity: int
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class OrderRequest:
    symbol: str
    side: OrderSide
    quantity: int
    estimated_price: float
    reason: str
    status: OrderStatus = OrderStatus.PENDING_CONFIRMATION
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    approved_at: str | None = None
    filled_at: str | None = None
    message: str = ""

    @property
    def estimated_notional(self) -> float:
        return round(self.quantity * self.estimated_price, 2)


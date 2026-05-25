from stockquan.models import OrderSide, OrderStatus, TradeSignal
from stockquan.orders import OrderBook


def test_order_requires_confirmation_before_execution(tmp_path):
    book = OrderBook(tmp_path / "orders.json")
    signal = TradeSignal(
        symbol="AAPL",
        side=OrderSide.BUY,
        confidence=0.7,
        reason="test",
        price=100,
        quantity=2,
    )

    order = book.create_from_signal(signal)
    loaded = book.list_orders()[0]

    assert order.id == loaded.id
    assert loaded.status == OrderStatus.PENDING_CONFIRMATION
    assert loaded.side == OrderSide.BUY


from __future__ import annotations

from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from stockquan.broker import PaperBroker, build_broker
from stockquan.config import settings
from stockquan.data import MarketDataClient
from stockquan.models import OrderStatus
from stockquan.news import NewsCrawler
from stockquan.orders import OrderBook
from stockquan.strategy import MovingAverageCrossStrategy


st.set_page_config(page_title="StockQuan", layout="wide")


@st.cache_resource
def market_client() -> MarketDataClient:
    return MarketDataClient(settings.data_dir / "market_cache")


@st.cache_resource
def order_book() -> OrderBook:
    return OrderBook(settings.data_dir / "orders.json")


def render_price_chart(history: pd.DataFrame, short_window: int, long_window: int) -> None:
    frame = history.copy()
    frame["short_ma"] = frame["close"].rolling(short_window).mean()
    frame["long_ma"] = frame["close"].rolling(long_window).mean()
    x_axis = frame["date"] if "date" in frame.columns else frame.index

    figure = go.Figure()
    figure.add_trace(go.Scatter(x=x_axis, y=frame["close"], name="Close", mode="lines"))
    figure.add_trace(go.Scatter(x=x_axis, y=frame["short_ma"], name=f"MA {short_window}", mode="lines"))
    figure.add_trace(go.Scatter(x=x_axis, y=frame["long_ma"], name=f"MA {long_window}", mode="lines"))
    figure.update_layout(height=430, margin=dict(l=10, r=10, t=30, b=10), legend=dict(orientation="h"))
    st.plotly_chart(figure, use_container_width=True)


def render_account() -> None:
    broker = build_broker(settings)
    st.subheader("賬戶")
    st.caption(f"交易模式：{settings.trading_mode}")
    if isinstance(broker, PaperBroker):
        st.metric("Paper Cash", f"${broker.cash:,.2f}")
        positions = broker.positions
        if positions:
            st.dataframe(
                pd.DataFrame([{"symbol": symbol, "quantity": quantity} for symbol, quantity in positions.items()]),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("暫無持倉")
    else:
        st.warning("真實交易 adapter 尚未配置。")


def render_orders() -> None:
    book = order_book()
    broker = build_broker(settings)
    orders = sorted(book.list_orders(), key=lambda order: order.created_at, reverse=True)

    st.subheader("待確認 / 歷史訂單")
    if not orders:
        st.info("暫無訂單")
        return

    for order in orders:
        with st.container(border=True):
            left, middle, right = st.columns([2, 2, 1])
            left.markdown(f"**{order.symbol} {order.side.value} x {order.quantity}**")
            left.caption(order.reason)
            middle.write(f"估算價格：${order.estimated_price:,.2f}")
            middle.write(f"估算金額：${order.estimated_notional:,.2f}")
            right.write(order.status.value)
            if order.message:
                st.caption(order.message)

            if order.status == OrderStatus.PENDING_CONFIRMATION:
                approve, reject = st.columns(2)
                if approve.button("確認並執行", key=f"approve-{order.id}", type="primary"):
                    approved = book.approve(order.id)
                    filled = broker.execute(approved)
                    book.update(filled)
                    st.rerun()
                if reject.button("拒絕", key=f"reject-{order.id}"):
                    book.reject(order.id)
                    st.rerun()


def render_news() -> None:
    st.subheader("新聞爬蟲")
    crawler = NewsCrawler(settings.news_rss_feeds)
    if not settings.news_rss_feeds:
        st.warning("請在 .env 設置 NEWS_RSS_FEEDS。")
        return

    if st.button("抓取最新新聞"):
        try:
            items = crawler.fetch(limit=20)
        except Exception as exc:
            st.error(f"新聞抓取失敗：{exc}")
            return
        if not items:
            st.info("沒有抓到新聞。")
        for item in items:
            st.markdown(f"**[{item.title}]({item.link})**")
            st.caption(f"{item.source} | {item.published}")


def main() -> None:
    st.title("StockQuan")

    sidebar = st.sidebar
    sidebar.header("策略參數")
    symbol = sidebar.text_input("股票代碼", value="AAPL").upper().strip()
    period = sidebar.selectbox("歷史區間", ["3mo", "6mo", "1y", "2y"], index=1)
    short_window = sidebar.slider("短均線", 5, 60, 20)
    long_window = sidebar.slider("長均線", 20, 200, 50)
    risk_fraction = sidebar.slider("單次風險比例", 0.01, 0.5, 0.1, step=0.01)

    if short_window >= long_window:
        st.error("短均線必須小於長均線。")
        return

    account_col, order_col = st.columns([1, 2])
    with account_col:
        render_account()
    with order_col:
        render_orders()

    st.divider()
    market_col, news_col = st.columns([2, 1])

    with market_col:
        st.subheader("行情與策略")
        if st.button("抓取行情並生成策略訊號", type="primary"):
            try:
                history = market_client().history(symbol, period=period)
                render_price_chart(history, short_window, long_window)
                broker = build_broker(settings)
                cash = broker.cash if isinstance(broker, PaperBroker) else settings.default_cash
                strategy = MovingAverageCrossStrategy(short_window, long_window, risk_fraction)
                signal = strategy.evaluate(symbol, history, cash=cash)
                if signal is None:
                    st.info("目前沒有交易訊號。")
                else:
                    order = order_book().create_from_signal(signal)
                    st.success(f"已生成待確認訂單：{order.symbol} {order.side.value} x {order.quantity}")
                    st.caption("訂單尚未執行，請在上方確認。")
            except Exception as exc:
                st.error(f"行情或策略處理失敗：{exc}")

    with news_col:
        render_news()


if __name__ == "__main__":
    main()


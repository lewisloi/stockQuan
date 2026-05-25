from __future__ import annotations

import csv
import html
import json
import math
import os
import random
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4
from xml.etree import ElementTree


DATA_DIR = Path("data")
MARKET_DIR = DATA_DIR / "market"
ORDERS_PATH = DATA_DIR / "orders.json"
ACCOUNT_PATH = DATA_DIR / "account.json"
NEWS_CACHE_PATH = DATA_DIR / "news_cache.json"
SCAN_STATE_PATH = DATA_DIR / "last_scan.json"
FILLS_CSV_PATH = DATA_DIR / "fills.csv"
AUTO_STATE_PATH = DATA_DIR / "auto_trader.json"
DEFAULT_CASH = 100000.0
MAX_ORDER_NOTIONAL = 10000.0
NEWS_CACHE_TTL_SECONDS = 900
NEWS_FEEDS = [
    "https://finance.yahoo.com/news/rssindex",
    "https://www.marketwatch.com/rss/topstories",
]
AUTO_STATE_LOCK = threading.Lock()


@dataclass
class Order:
    symbol: str
    side: str
    quantity: int
    estimated_price: float
    reason: str
    status: str = "PENDING_CONFIRMATION"
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    approved_at: str = ""
    filled_at: str = ""
    message: str = ""

    @property
    def notional(self) -> float:
        return round(self.quantity * self.estimated_price, 2)


@dataclass
class MarketData:
    symbol: str
    dates: list[str]
    prices: list[float]
    source: str
    fetched_at: str
    error: str = ""

    @property
    def latest_price(self) -> float:
        return float(self.prices[-1])


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(value, file, indent=2, ensure_ascii=False)


def load_account() -> dict:
    account = read_json(ACCOUNT_PATH, {"cash": DEFAULT_CASH, "positions": {}, "fills": []})
    account.setdefault("cash", DEFAULT_CASH)
    account.setdefault("positions", {})
    account.setdefault("fills", [])
    return account


def save_account(account: dict) -> None:
    write_json(ACCOUNT_PATH, account)


def load_orders() -> list[Order]:
    return [Order(**item) for item in read_json(ORDERS_PATH, [])]


def save_orders(orders: list[Order]) -> None:
    write_json(ORDERS_PATH, [asdict(order) for order in orders])


def has_pending_order(symbol: str, side: str) -> bool:
    return any(
        order.symbol == symbol and order.side == side and order.status == "PENDING_CONFIRMATION"
        for order in load_orders()
    )


def append_fill(order: Order) -> None:
    FILLS_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    exists = FILLS_CSV_PATH.exists()
    with FILLS_CSV_PATH.open("a", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=["filled_at", "id", "symbol", "side", "quantity", "price", "notional", "reason"],
        )
        if not exists:
            writer.writeheader()
        writer.writerow(
            {
                "filled_at": order.filled_at,
                "id": order.id,
                "symbol": order.symbol,
                "side": order.side,
                "quantity": order.quantity,
                "price": order.estimated_price,
                "notional": order.notional,
                "reason": order.reason,
            }
        )


def fetch_market_data(symbol: str) -> MarketData:
    symbol = symbol.upper().strip()
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?range=6mo&interval=1d"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "StockQuan/0.1"})
        with urllib.request.urlopen(request, timeout=6) as response:
            payload = json.loads(response.read().decode("utf-8"))
        result = payload["chart"]["result"][0]
        timestamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
        dates: list[str] = []
        prices: list[float] = []
        for timestamp, close in zip(timestamps, closes):
            if close is None:
                continue
            dates.append(datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat())
            prices.append(round(float(close), 4))
        if len(prices) < 60:
            raise ValueError("Yahoo returned too few daily prices.")
        market = MarketData(symbol=symbol, dates=dates, prices=prices, source="Yahoo Finance chart API", fetched_at=now_iso())
    except Exception as exc:
        market = demo_market_data(symbol, str(exc))
    cache_market_data(market)
    return market


def demo_market_data(symbol: str, error: str) -> MarketData:
    seed = sum(ord(char) for char in symbol)
    rng = random.Random(seed)
    prices: list[float] = []
    dates: list[str] = []
    price = 85 + seed % 70
    for index in range(140):
        drift = math.sin(index / 7) * 0.9 + 0.08
        price = max(5.0, price + drift + rng.uniform(-1.25, 1.25))
        prices.append(round(price, 2))
        dates.append(f"D-{139 - index}")
    return MarketData(
        symbol=symbol,
        dates=dates,
        prices=prices,
        source="Demo deterministic market data",
        fetched_at=now_iso(),
        error=error,
    )


def cache_market_data(market: MarketData) -> None:
    write_json(MARKET_DIR / f"{market.symbol}.json", asdict(market))
    with (MARKET_DIR / f"{market.symbol}.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["date", "close"])
        writer.writeheader()
        for date, price in zip(market.dates, market.prices):
            writer.writerow({"date": date, "close": price})


def moving_average(values: list[float], window: int) -> list[float | None]:
    result: list[float | None] = []
    running_sum = 0.0
    for index, value in enumerate(values):
        running_sum += value
        if index >= window:
            running_sum -= values[index - window]
        if index + 1 < window:
            result.append(None)
        else:
            result.append(running_sum / window)
    return result


def analyze_market(market: MarketData, account: dict) -> dict:
    prices = market.prices
    short = moving_average(prices, 20)
    long = moving_average(prices, 50)
    latest = prices[-1]
    short_latest = short[-1] or latest
    long_latest = long[-1] or latest
    momentum = ((prices[-1] - prices[-6]) / prices[-6]) * 100 if len(prices) >= 6 else 0
    position = int(account.get("positions", {}).get(market.symbol, 0))

    signal = "HOLD"
    reason = "未達到買入或賣出條件"
    if latest > long_latest and momentum > 0:
        signal = "BUY"
        reason = "價格高於50日均線且5日動能為正"
    elif position > 0 and (latest < long_latest or momentum < -2):
        signal = "SELL"
        reason = "持倉中，價格跌破50日均線或5日動能轉弱"

    return {
        "symbol": market.symbol,
        "latest": latest,
        "short_ma": short_latest,
        "long_ma": long_latest,
        "momentum": momentum,
        "signal": signal,
        "reason": reason,
        "source": market.source,
        "error": market.error,
        "fetched_at": market.fetched_at,
        "chart_points": build_chart_points(prices[-80:]),
    }


def build_chart_points(prices: list[float]) -> str:
    if not prices:
        return ""
    low = min(prices)
    high = max(prices)
    span = high - low or 1
    points = []
    for index, price in enumerate(prices):
        x = 8 + index * (564 / max(len(prices) - 1, 1))
        y = 150 - ((price - low) / span) * 130
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


def create_order_from_scan(symbol: str, quantity: int) -> tuple[str, dict]:
    account = load_account()
    market = fetch_market_data(symbol)
    analysis = analyze_market(market, account)
    write_json(SCAN_STATE_PATH, {"market": asdict(market), "analysis": analysis})

    if analysis["signal"] == "HOLD":
        return "已更新行情和策略分析：目前是 HOLD，沒有建立訂單。", analysis

    order = Order(
        symbol=market.symbol,
        side=analysis["signal"],
        quantity=max(quantity, 1),
        estimated_price=market.latest_price,
        reason=analysis["reason"],
    )
    orders = load_orders()
    orders.append(order)
    save_orders(orders)
    return f"已建立待確認訂單：{order.symbol} {order.side} x {order.quantity}，尚未成交。", analysis


def auto_scan_watchlist(watchlist: str) -> str:
    symbols = [item.strip().upper() for item in watchlist.split(",") if item.strip()]
    if not symbols:
        return "Watchlist 為空。"
    messages = []
    account = load_account()
    for symbol in symbols:
        market = fetch_market_data(symbol)
        analysis = analyze_market(market, account)
        if analysis["signal"] == "HOLD":
            messages.append(f"{symbol}: HOLD")
            continue
        if has_pending_order(symbol, analysis["signal"]):
            messages.append(f"{symbol}: 已有待確認 {analysis['signal']} 訂單")
            continue
        order = Order(
            symbol=symbol,
            side=analysis["signal"],
            quantity=max(int((account["cash"] * 0.05) // market.latest_price), 1),
            estimated_price=market.latest_price,
            reason=f"自動掃描：{analysis['reason']}",
        )
        orders = load_orders()
        orders.append(order)
        save_orders(orders)
        messages.append(f"{symbol}: 已建立待確認 {order.side} 訂單")
    return "；".join(messages)


def load_auto_state() -> dict:
    state = read_json(AUTO_STATE_PATH, {"enabled": False, "watchlist": "AAPL,MSFT,NVDA,TSLA", "interval": 300, "last_run": "", "last_message": ""})
    state.setdefault("enabled", False)
    state.setdefault("watchlist", "AAPL,MSFT,NVDA,TSLA")
    state.setdefault("interval", 300)
    state.setdefault("last_run", "")
    state.setdefault("last_message", "")
    return state


def save_auto_state(state: dict) -> None:
    with AUTO_STATE_LOCK:
        write_json(AUTO_STATE_PATH, state)


def set_auto_trader(enabled: bool, watchlist: str, interval: int) -> str:
    interval = max(interval, 60)
    state = load_auto_state()
    state.update({"enabled": enabled, "watchlist": watchlist, "interval": interval})
    save_auto_state(state)
    return "自動監控已啟動。" if enabled else "自動監控已停止。"


def auto_trader_loop() -> None:
    while True:
        state = load_auto_state()
        if state.get("enabled"):
            message = auto_scan_watchlist(state.get("watchlist", ""))
            state["last_run"] = now_iso()
            state["last_message"] = message
            save_auto_state(state)
            time.sleep(max(int(state.get("interval", 300)), 60))
        else:
            time.sleep(3)


def execute_order(order_id: str) -> str:
    orders = load_orders()
    account = load_account()
    message = "找不到可執行訂單。"
    for order in orders:
        if order.id != order_id or order.status != "PENDING_CONFIRMATION":
            continue
        if order.notional > MAX_ORDER_NOTIONAL:
            order.status = "FAILED"
            order.message = f"訂單金額 ${order.notional:,.2f} 超過風控上限 ${MAX_ORDER_NOTIONAL:,.2f}"
            message = order.message
            break
        positions = account.setdefault("positions", {})
        current = int(positions.get(order.symbol, 0))
        if order.side == "BUY":
            if account["cash"] < order.notional:
                order.status = "FAILED"
                order.message = "Paper cash 不足"
                message = order.message
                break
            account["cash"] = round(account["cash"] - order.notional, 2)
            positions[order.symbol] = current + order.quantity
        else:
            if current < order.quantity:
                order.status = "FAILED"
                order.message = "Paper position 不足"
                message = order.message
                break
            account["cash"] = round(account["cash"] + order.notional, 2)
            positions[order.symbol] = current - order.quantity
        order.status = "FILLED"
        order.approved_at = now_iso()
        order.filled_at = now_iso()
        order.message = "Paper broker 已成交"
        account.setdefault("fills", []).append(asdict(order))
        append_fill(order)
        message = f"已成交：{order.symbol} {order.side} x {order.quantity}"
        break
    save_orders(orders)
    save_account(account)
    return message


def reject_order(order_id: str) -> str:
    orders = load_orders()
    for order in orders:
        if order.id == order_id and order.status == "PENDING_CONFIRMATION":
            order.status = "REJECTED"
            order.message = "用戶拒絕"
            save_orders(orders)
            return f"已拒絕訂單：{order.symbol} {order.side} x {order.quantity}"
    return "找不到可拒絕訂單。"


def fetch_news(force: bool = False) -> list[dict[str, str]]:
    cached = read_json(NEWS_CACHE_PATH, {"fetched_at": 0, "items": []})
    if not force and time.time() - cached.get("fetched_at", 0) < NEWS_CACHE_TTL_SECONDS:
        return cached.get("items", [])

    items = []
    for feed in NEWS_FEEDS:
        try:
            request = urllib.request.Request(feed, headers={"User-Agent": "StockQuan/0.1"})
            with urllib.request.urlopen(request, timeout=5) as response:
                root = ElementTree.fromstring(response.read())
            for item in root.findall(".//item")[:8]:
                items.append(
                    {
                        "title": item.findtext("title", "Untitled"),
                        "link": item.findtext("link", "#"),
                        "date": item.findtext("pubDate", ""),
                    }
                )
        except Exception as exc:
            items.append({"title": f"新聞源暫時不可用：{feed}", "link": "#", "date": str(exc)})
    items = items[:16]
    write_json(NEWS_CACHE_PATH, {"fetched_at": time.time(), "items": items})
    return items


def latest_scan() -> dict:
    value = read_json(SCAN_STATE_PATH, {})
    if value:
        return value
    market = demo_market_data("AAPL", "尚未抓取行情")
    return {"market": asdict(market), "analysis": analyze_market(market, load_account())}


def render_orders(orders: list[Order]) -> str:
    rows = []
    for order in sorted(orders, key=lambda item: item.created_at, reverse=True):
        actions = ""
        if order.status == "PENDING_CONFIRMATION":
            actions = f"""
            <form method="post" action="/approve" class="inline">
              <input type="hidden" name="id" value="{html.escape(order.id)}">
              <button>確認並執行</button>
            </form>
            <form method="post" action="/reject" class="inline">
              <input type="hidden" name="id" value="{html.escape(order.id)}">
              <button class="secondary">拒絕</button>
            </form>
            """
        rows.append(
            "<tr>"
            f"<td>{html.escape(order.symbol)}</td><td>{order.side}</td><td>{order.quantity}</td>"
            f"<td>${order.estimated_price:,.2f}</td><td>${order.notional:,.2f}</td>"
            f"<td><span class='status'>{order.status}</span></td>"
            f"<td>{html.escape(order.reason)}<small>{html.escape(order.message)}</small></td>"
            f"<td>{actions}</td>"
            "</tr>"
        )
    return "".join(rows) or "<tr><td colspan='8'>暫無訂單</td></tr>"


def render_page(message: str = "") -> bytes:
    account = load_account()
    orders = load_orders()
    scan = latest_scan()
    auto_state = load_auto_state()
    analysis = scan["analysis"]
    market = scan["market"]
    news = fetch_news()

    positions_html = "".join(
        f"<li>{html.escape(symbol)}: {quantity}</li>" for symbol, quantity in account.get("positions", {}).items() if quantity
    ) or "<li>暫無持倉</li>"
    fills_html = "".join(
        f"<tr><td>{html.escape(fill['symbol'])}</td><td>{fill['side']}</td><td>{fill['quantity']}</td>"
        f"<td>${float(fill['estimated_price']):,.2f}</td><td>{html.escape(fill.get('filled_at', ''))}</td></tr>"
        for fill in account.get("fills", [])[-10:][::-1]
    ) or "<tr><td colspan='5'>暫無成交</td></tr>"
    news_html = "".join(
        f"<li><a href='{html.escape(item['link'])}' target='_blank'>{html.escape(item['title'])}</a><small>{html.escape(item['date'])}</small></li>"
        for item in news
    ) or "<li>暫無新聞</li>"
    market_rows = "".join(
        f"<tr><td>{html.escape(date)}</td><td>${float(price):,.2f}</td></tr>"
        for date, price in list(zip(market["dates"], market["prices"]))[-8:][::-1]
    )
    warning = ""
    if analysis.get("error"):
        warning = f"<p class='warn'>行情接口不可用時已使用 demo 數據：{html.escape(analysis['error'])}</p>"

    body = f"""
    <!doctype html>
    <html lang="zh-Hant">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>StockQuan</title>
      <style>
        body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f4f6f8; color: #17202a; }}
        header {{ background: #143642; color: white; padding: 18px 28px; display: flex; justify-content: space-between; gap: 12px; align-items: end; }}
        main {{ padding: 20px; display: grid; gap: 16px; grid-template-columns: 1.4fr 0.9fr; }}
        section {{ background: white; border: 1px solid #d8dee6; border-radius: 8px; padding: 16px; }}
        h1, h2, h3 {{ margin: 0 0 12px; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
        th, td {{ border-bottom: 1px solid #e6e9ee; padding: 9px; text-align: left; vertical-align: top; }}
        input, button {{ padding: 9px 11px; border-radius: 6px; border: 1px solid #b8c0cc; font-size: 14px; }}
        button {{ background: #18745a; color: white; border: 0; cursor: pointer; }}
        button.secondary {{ background: #667085; }}
        .inline {{ display: inline; margin-right: 6px; }}
        .grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-bottom: 14px; }}
        .metric {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 10px; }}
        .metric strong {{ display: block; font-size: 18px; margin-top: 4px; }}
        .notice {{ color: #18745a; font-weight: 700; }}
        .warn {{ color: #9a3412; background: #fff7ed; padding: 8px; border-radius: 6px; }}
        .status {{ font-weight: 700; }}
        small {{ display: block; color: #64748b; margin-top: 4px; }}
        svg {{ width: 100%; height: 170px; background: #fbfdff; border: 1px solid #e2e8f0; border-radius: 8px; }}
        ul {{ padding-left: 18px; }}
        @media (max-width: 950px) {{ main {{ grid-template-columns: 1fr; padding: 12px; }} .grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} }}
      </style>
    </head>
    <body>
      <header>
        <div><h1>StockQuan</h1><div>Paper trading | 策略自動生成待確認訂單 | 用戶確認後才成交</div></div>
        <div>風控上限：${MAX_ORDER_NOTIONAL:,.0f} / order</div>
      </header>
      <main>
        <section>
          <h2>行情、策略、訂單生成</h2>
          <form method="post" action="/scan">
            <input name="symbol" value="{html.escape(analysis['symbol'])}" placeholder="股票代碼">
            <input name="quantity" type="number" min="1" value="10">
            <button>抓取行情並生成訊號</button>
          </form>
          <p class="notice">{html.escape(message)}</p>
          {warning}
          <div class="grid">
            <div class="metric">股票<strong>{html.escape(analysis['symbol'])}</strong></div>
            <div class="metric">最新價<strong>${analysis['latest']:,.2f}</strong></div>
            <div class="metric">策略訊號<strong>{analysis['signal']}</strong></div>
            <div class="metric">5日動能<strong>{analysis['momentum']:.2f}%</strong></div>
          </div>
          <svg viewBox="0 0 580 170" role="img" aria-label="price chart">
            <polyline fill="none" stroke="#18745a" stroke-width="3" points="{analysis['chart_points']}"></polyline>
          </svg>
          <small>數據源：{html.escape(analysis['source'])} | 更新：{html.escape(analysis['fetched_at'])} | 原因：{html.escape(analysis['reason'])}</small>
          <h3>最近行情</h3>
          <table><thead><tr><th>日期</th><th>收盤價</th></tr></thead><tbody>{market_rows}</tbody></table>

          <h2>待確認 / 歷史訂單</h2>
          <table>
            <thead><tr><th>股票</th><th>方向</th><th>數量</th><th>價格</th><th>金額</th><th>狀態</th><th>原因</th><th>操作</th></tr></thead>
            <tbody>{render_orders(orders)}</tbody>
          </table>
        </section>

        <section>
          <h2>自動掃描</h2>
          <form method="post" action="/auto-scan">
            <input name="watchlist" value="{html.escape(auto_state['watchlist'])}">
            <button>掃描 Watchlist</button>
          </form>
          <small>自動掃描只建立待確認訂單，不會直接成交。</small>
          <h3>自動監控</h3>
          <form method="post" action="/auto-start" class="inline">
            <input name="watchlist" value="{html.escape(auto_state['watchlist'])}">
            <input name="interval" type="number" min="60" value="{int(auto_state['interval'])}">
            <button>啟動</button>
          </form>
          <form method="post" action="/auto-stop" class="inline"><button class="secondary">停止</button></form>
          <small>狀態：{'ON' if auto_state['enabled'] else 'OFF'} | 上次：{html.escape(auto_state.get('last_run', '') or '-')} | {html.escape(auto_state.get('last_message', '') or '尚未執行')}</small>

          <h2>Paper 賬戶</h2>
          <p>Cash: <strong>${float(account["cash"]):,.2f}</strong></p>
          <ul>{positions_html}</ul>

          <h2>成交記錄</h2>
          <table><thead><tr><th>股票</th><th>方向</th><th>數量</th><th>價格</th><th>時間</th></tr></thead><tbody>{fills_html}</tbody></table>

          <h2>新聞爬蟲</h2>
          <form method="post" action="/news"><button class="secondary">更新新聞</button></form>
          <ul>{news_html}</ul>
        </section>
      </main>
    </body>
    </html>
    """
    return body.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.respond(render_page())

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        data = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
        if self.path == "/scan":
            symbol = data.get("symbol", ["AAPL"])[0].strip().upper() or "AAPL"
            quantity = int(data.get("quantity", ["10"])[0] or "10")
            message, _analysis = create_order_from_scan(symbol, quantity)
            self.respond(render_page(message))
            return
        if self.path == "/auto-scan":
            message = auto_scan_watchlist(data.get("watchlist", ["AAPL,MSFT,NVDA"])[0])
            self.respond(render_page(message))
            return
        if self.path == "/auto-start":
            watchlist = data.get("watchlist", ["AAPL,MSFT,NVDA,TSLA"])[0]
            interval = int(data.get("interval", ["300"])[0] or "300")
            self.respond(render_page(set_auto_trader(True, watchlist, interval)))
            return
        if self.path == "/auto-stop":
            state = load_auto_state()
            self.respond(render_page(set_auto_trader(False, state.get("watchlist", ""), int(state.get("interval", 300)))))
            return
        if self.path == "/approve":
            self.respond(render_page(execute_order(data.get("id", [""])[0])))
            return
        if self.path == "/reject":
            self.respond(render_page(reject_order(data.get("id", [""])[0])))
            return
        if self.path == "/news":
            fetch_news(force=True)
            self.respond(render_page("新聞已更新。"))
            return
        self.respond(render_page())

    def log_message(self, format: str, *args) -> None:
        return

    def respond(self, payload: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    DATA_DIR.mkdir(exist_ok=True)
    MARKET_DIR.mkdir(exist_ok=True)
    ThreadingHTTPServer.allow_reuse_address = True
    port = int(os.getenv("PORT", "8502"))
    threading.Thread(target=auto_trader_loop, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"StockQuan is running at http://127.0.0.1:{port}")
    server.serve_forever()

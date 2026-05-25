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
WATCHLIST_PATH = DATA_DIR / "watchlist.json"
RECOMMENDATIONS_PATH = DATA_DIR / "recommendations.json"
LLM_STATE_PATH = DATA_DIR / "last_llm_answer.json"
DEFAULT_CASH = 100000.0
MAX_ORDER_NOTIONAL = 10000.0
NEWS_CACHE_TTL_SECONDS = 900
DEFAULT_WATCHLIST = ["AAPL", "MSFT", "NVDA", "TSLA"]
RECOMMEND_UNIVERSE = [
    "AAPL",
    "MSFT",
    "NVDA",
    "GOOGL",
    "AMZN",
    "META",
    "TSLA",
    "AMD",
    "AVGO",
    "NFLX",
    "JPM",
    "V",
    "MA",
    "COST",
    "LLY",
    "UNH",
]
NEWS_FEEDS = [
    "https://finance.yahoo.com/news/rssindex",
    "https://www.marketwatch.com/rss/topstories",
]
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
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


def normalize_symbol(symbol: str) -> str:
    return "".join(char for char in symbol.upper().strip() if char.isalnum() or char in ".-")[:12]


def load_watchlist() -> list[str]:
    value = read_json(WATCHLIST_PATH, {"symbols": DEFAULT_WATCHLIST})
    symbols = value.get("symbols", DEFAULT_WATCHLIST) if isinstance(value, dict) else value
    normalized = []
    for symbol in symbols:
        clean = normalize_symbol(str(symbol))
        if clean and clean not in normalized:
            normalized.append(clean)
    return normalized or DEFAULT_WATCHLIST.copy()


def save_watchlist(symbols: list[str]) -> None:
    normalized = []
    for symbol in symbols:
        clean = normalize_symbol(symbol)
        if clean and clean not in normalized:
            normalized.append(clean)
    write_json(WATCHLIST_PATH, {"symbols": normalized, "updated_at": now_iso()})


def add_to_watchlist(symbol: str) -> str:
    clean = normalize_symbol(symbol)
    if not clean:
        return "股票代碼無效。"
    symbols = load_watchlist()
    if clean in symbols:
        return f"{clean} 已在 watchlist。"
    symbols.append(clean)
    save_watchlist(symbols)
    return f"已加入 watchlist：{clean}"


def remove_from_watchlist(symbol: str) -> str:
    clean = normalize_symbol(symbol)
    symbols = [item for item in load_watchlist() if item != clean]
    save_watchlist(symbols)
    return f"已從 watchlist 移除：{clean}"


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
    trend_pct = ((latest - long_latest) / long_latest) * 100 if long_latest else 0
    short_trend_pct = ((latest - short_latest) / short_latest) * 100 if short_latest else 0
    position = int(account.get("positions", {}).get(market.symbol, 0))

    signal = "HOLD"
    reason = "未達到買入或賣出條件"
    if latest > long_latest and momentum > 0:
        signal = "BUY"
        reason = "價格高於50日均線且5日動能為正"
    elif position > 0 and (latest < long_latest or momentum < -2):
        signal = "SELL"
        reason = "持倉中，價格跌破50日均線或5日動能轉弱"

    score = round((momentum * 2.0) + trend_pct + (short_trend_pct * 0.5), 2)
    if signal == "BUY":
        score += 12
    elif signal == "SELL":
        score -= 20
    if position > 0 and signal != "SELL":
        score += 2

    return {
        "symbol": market.symbol,
        "latest": latest,
        "short_ma": short_latest,
        "long_ma": long_latest,
        "momentum": momentum,
        "trend_pct": trend_pct,
        "short_trend_pct": short_trend_pct,
        "score": round(score, 2),
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


def recommendation_universe() -> list[str]:
    symbols = []
    for symbol in load_watchlist() + RECOMMEND_UNIVERSE:
        clean = normalize_symbol(symbol)
        if clean and clean not in symbols:
            symbols.append(clean)
    return symbols


def refresh_recommendations() -> list[dict]:
    account = load_account()
    watchlist = set(load_watchlist())
    rows = []
    for symbol in recommendation_universe():
        market = fetch_market_data(symbol)
        analysis = analyze_market(market, account)
        rows.append(
            {
                "symbol": symbol,
                "latest": analysis["latest"],
                "score": analysis["score"],
                "signal": analysis["signal"],
                "momentum": analysis["momentum"],
                "trend_pct": analysis["trend_pct"],
                "reason": analysis["reason"],
                "watched": symbol in watchlist,
                "source": analysis["source"],
                "fetched_at": analysis["fetched_at"],
                "error": analysis["error"],
            }
        )
    rows.sort(key=lambda row: row["score"], reverse=True)
    write_json(RECOMMENDATIONS_PATH, {"updated_at": now_iso(), "items": rows})
    return rows


def load_recommendations() -> dict:
    cached = read_json(RECOMMENDATIONS_PATH, {})
    if cached.get("items"):
        watchlist = set(load_watchlist())
        for item in cached["items"]:
            item["watched"] = item.get("symbol") in watchlist
        return cached
    return {"updated_at": now_iso(), "items": refresh_recommendations()}


def build_theory_judgements(analysis: dict) -> list[dict[str, str]]:
    latest = analysis["latest"]
    short_ma = analysis["short_ma"]
    long_ma = analysis["long_ma"]
    momentum = analysis["momentum"]
    trend_pct = analysis["trend_pct"]
    score = analysis["score"]
    judgements = []

    trend_action = "BUY" if latest > long_ma and latest > short_ma else "SELL" if latest < long_ma else "HOLD"
    judgements.append(
        {
            "theory": "趨勢跟隨",
            "action": trend_action,
            "reason": f"現價相對50日均線 {trend_pct:.2f}%，現價 {'高於' if latest > long_ma else '低於'} 50日均線。",
        }
    )

    momentum_action = "BUY" if momentum > 2 else "SELL" if momentum < -2 else "HOLD"
    judgements.append(
        {
            "theory": "動量理論",
            "action": momentum_action,
            "reason": f"5日動能為 {momentum:.2f}%，{'動能偏強' if momentum > 2 else '動能偏弱' if momentum < -2 else '動能不明顯'}。",
        }
    )

    distance_to_short = ((latest - short_ma) / short_ma) * 100 if short_ma else 0
    mean_action = "SELL" if distance_to_short > 8 else "BUY" if distance_to_short < -8 else "HOLD"
    judgements.append(
        {
            "theory": "均值回歸",
            "action": mean_action,
            "reason": f"現價相對20日均線 {distance_to_short:.2f}%，偏離過大時傾向等待回歸。",
        }
    )

    score_action = "BUY" if score >= 15 else "SELL" if score <= -8 else "HOLD"
    judgements.append(
        {
            "theory": "多因子評分",
            "action": score_action,
            "reason": f"綜合分數 {score:.2f}，由動能、20/50日均線位置和策略訊號組成。",
        }
    )

    risk_action = "HOLD" if analysis["signal"] == "BUY" and score < 25 else analysis["signal"]
    judgements.append(
        {
            "theory": "風險控制",
            "action": risk_action,
            "reason": f"單筆訂單仍受 ${MAX_ORDER_NOTIONAL:,.0f} 風控上限限制；訊號不足強時不追價。",
        }
    )
    return judgements


def aggregate_theory_action(judgements: list[dict[str, str]]) -> str:
    weights = {"BUY": 1, "HOLD": 0, "SELL": -1}
    total = sum(weights.get(item["action"], 0) for item in judgements)
    if total >= 2:
        return "BUY"
    if total <= -2:
        return "SELL"
    return "HOLD"


def local_stock_answer(symbol: str, question: str, analysis: dict, judgements: list[dict[str, str]]) -> str:
    final_action = aggregate_theory_action(judgements)
    theory_lines = "\n".join(
        f"- {item['theory']}: {item['action']}。{item['reason']}" for item in judgements
    )
    return (
        f"{symbol} 本地多理論分析結論：{final_action}\n\n"
        f"你的問題：{question or '是否應該買入或賣出？'}\n\n"
        f"行情摘要：最新價 ${analysis['latest']:,.2f}，20日均線 ${analysis['short_ma']:,.2f}，"
        f"50日均線 ${analysis['long_ma']:,.2f}，5日動能 {analysis['momentum']:.2f}%，"
        f"系統分數 {analysis['score']:.2f}。\n\n"
        f"不同理論判斷：\n{theory_lines}\n\n"
        "執行建議：即使結論是 BUY 或 SELL，系統也只會生成待確認訂單；你仍需在訂單區手動確認。"
    )


def call_openai_llm(symbol: str, question: str, analysis: dict, judgements: list[dict[str, str]]) -> str:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured.")
    prompt = {
        "symbol": symbol,
        "question": question,
        "market_analysis": analysis,
        "theory_judgements": judgements,
        "required_output": "用繁體中文回答，明確給出 BUY/HOLD/SELL，解釋不同理論分歧和風險，不可承諾收益。",
    }
    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {
                "role": "system",
                "content": "你是股票量化分析助手。只根據提供的行情和理論判斷回答，不要編造未提供的財報或新聞。回答不是財務建議。",
            },
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ],
        "temperature": 0.2,
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.loads(response.read().decode("utf-8"))
    return result["choices"][0]["message"]["content"]


def ask_stock_assistant(symbol: str, question: str) -> str:
    clean = normalize_symbol(symbol) or "AAPL"
    market = fetch_market_data(clean)
    analysis = analyze_market(market, load_account())
    judgements = build_theory_judgements(analysis)
    provider = "local"
    try:
        answer = call_openai_llm(clean, question, analysis, judgements)
        provider = f"openai:{OPENAI_MODEL}"
    except Exception as exc:
        answer = local_stock_answer(clean, question, analysis, judgements)
        if OPENAI_API_KEY:
            answer += f"\n\nLLM API 暫時不可用，已改用本地分析。錯誤：{exc}"

    state = {
        "symbol": clean,
        "question": question,
        "answer": answer,
        "provider": provider,
        "analysis": analysis,
        "judgements": judgements,
        "created_at": now_iso(),
    }
    write_json(LLM_STATE_PATH, state)
    write_json(SCAN_STATE_PATH, {"market": asdict(market), "analysis": analysis})
    return f"已完成 {clean} 股票分析。"


def load_llm_state() -> dict:
    return read_json(
        LLM_STATE_PATH,
        {
            "symbol": "",
            "question": "",
            "answer": "尚未提問。輸入股票和問題後，系統會用趨勢跟隨、動量、均值回歸、多因子和風控框架判斷。",
            "provider": "local",
            "analysis": {},
            "judgements": [],
            "created_at": "",
        },
    )


def auto_scan_watchlist(watchlist: str) -> str:
    symbols = [normalize_symbol(item) for item in watchlist.split(",") if normalize_symbol(item)]
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
    default_watchlist = ",".join(load_watchlist())
    state = read_json(AUTO_STATE_PATH, {"enabled": False, "watchlist": default_watchlist, "interval": 300, "last_run": "", "last_message": ""})
    state.setdefault("enabled", False)
    state.setdefault("watchlist", default_watchlist)
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


def render_watchlist(symbols: list[str]) -> str:
    rows = []
    for symbol in symbols:
        rows.append(
            "<tr>"
            f"<td><strong>{html.escape(symbol)}</strong></td>"
            "<td>"
            f"<form method='post' action='/scan' class='inline'><input type='hidden' name='symbol' value='{html.escape(symbol)}'><input type='hidden' name='quantity' value='10'><button>掃描</button></form>"
            f"<form method='post' action='/watchlist-remove' class='inline'><input type='hidden' name='symbol' value='{html.escape(symbol)}'><button class='secondary'>移除</button></form>"
            "</td>"
            "</tr>"
        )
    return "".join(rows) or "<tr><td colspan='2'>暫無 watchlist</td></tr>"


def render_recommendations(recommendations: list[dict]) -> str:
    rows = []
    for index, item in enumerate(recommendations[:12], start=1):
        badge = "已關注" if item.get("watched") else "加入"
        add_action = ""
        if not item.get("watched"):
            add_action = (
                f"<form method='post' action='/watchlist-add' class='inline'>"
                f"<input type='hidden' name='symbol' value='{html.escape(item['symbol'])}'>"
                f"<button>{badge}</button></form>"
            )
        else:
            add_action = f"<span class='status'>{badge}</span>"
        rows.append(
            "<tr>"
            f"<td>{index}</td>"
            f"<td><strong>{html.escape(item['symbol'])}</strong></td>"
            f"<td>{item['score']:,.2f}</td>"
            f"<td>{item['signal']}</td>"
            f"<td>${item['latest']:,.2f}</td>"
            f"<td>{item['momentum']:.2f}%</td>"
            f"<td>{item['trend_pct']:.2f}%</td>"
            f"<td>{html.escape(item['reason'])}<small>{html.escape(item.get('source', ''))}</small></td>"
            f"<td>{add_action}</td>"
            "</tr>"
        )
    return "".join(rows) or "<tr><td colspan='9'>暫無推薦</td></tr>"


def render_theory_rows(judgements: list[dict[str, str]]) -> str:
    rows = []
    for item in judgements:
        rows.append(
            "<tr>"
            f"<td>{html.escape(item['theory'])}</td>"
            f"<td><span class='status'>{html.escape(item['action'])}</span></td>"
            f"<td>{html.escape(item['reason'])}</td>"
            "</tr>"
        )
    return "".join(rows) or "<tr><td colspan='3'>尚未分析</td></tr>"


def format_multiline_text(value: str) -> str:
    return "<br>".join(html.escape(value).splitlines())


def render_page(message: str = "") -> bytes:
    account = load_account()
    orders = load_orders()
    scan = latest_scan()
    auto_state = load_auto_state()
    watchlist = load_watchlist()
    recommendations = load_recommendations()
    llm_state = load_llm_state()
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
        input[type="hidden"] {{ display: none; }}
        button {{ background: #18745a; color: white; border: 0; cursor: pointer; }}
        button.secondary {{ background: #667085; }}
        .inline {{ display: inline; margin-right: 6px; }}
        .grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-bottom: 14px; }}
        .metric {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 10px; }}
        .metric strong {{ display: block; font-size: 18px; margin-top: 4px; }}
        .notice {{ color: #18745a; font-weight: 700; }}
        .warn {{ color: #9a3412; background: #fff7ed; padding: 8px; border-radius: 6px; }}
        .status {{ font-weight: 700; }}
        .wide {{ grid-column: 1 / -1; }}
        .answer {{ white-space: normal; line-height: 1.5; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 12px; }}
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

          <h2>LLM 股票分析助手</h2>
          <form method="post" action="/llm-ask">
            <input name="symbol" value="{html.escape(llm_state.get('symbol') or analysis['symbol'])}" placeholder="股票代碼">
            <input name="question" value="{html.escape(llm_state.get('question') or '這隻股票現在應該買入還是賣出？')}" placeholder="輸入你的問題">
            <button>分析股票</button>
          </form>
          <small>Provider：{html.escape(llm_state.get('provider', 'local'))} | 時間：{html.escape(llm_state.get('created_at', '') or '-')}</small>
          <div class="answer">{format_multiline_text(llm_state.get('answer', ''))}</div>
          <table>
            <thead><tr><th>理論</th><th>判斷</th><th>原因</th></tr></thead>
            <tbody>{render_theory_rows(llm_state.get('judgements', []))}</tbody>
          </table>
        </section>

        <section>
          <h2>Watchlist</h2>
          <form method="post" action="/watchlist-add">
            <input name="symbol" placeholder="輸入股票代碼，例如 AAPL">
            <button>加入 Watchlist</button>
          </form>
          <table><thead><tr><th>股票</th><th>操作</th></tr></thead><tbody>{render_watchlist(watchlist)}</tbody></table>

          <h2>Recommend List</h2>
          <form method="post" action="/recommend-refresh"><button>刷新推薦排行</button></form>
          <small>更新：{html.escape(recommendations.get('updated_at', '-'))}。分數越高越推薦，根據動能、均線趨勢、策略訊號排序。</small>
          <table>
            <thead><tr><th>#</th><th>股票</th><th>分數</th><th>訊號</th><th>價格</th><th>5日動能</th><th>對50MA</th><th>原因</th><th>Watch</th></tr></thead>
            <tbody>{render_recommendations(recommendations.get('items', []))}</tbody>
          </table>

          <h2>自動掃描</h2>
          <form method="post" action="/auto-scan">
            <input type="hidden" name="watchlist" value="{html.escape(','.join(watchlist))}">
            <button>掃描 Watchlist</button>
          </form>
          <small>自動掃描只建立待確認訂單，不會直接成交。</small>
          <h3>自動監控</h3>
          <form method="post" action="/auto-start" class="inline">
            <input type="hidden" name="watchlist" value="{html.escape(','.join(watchlist))}">
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
            message = auto_scan_watchlist(data.get("watchlist", [",".join(load_watchlist())])[0])
            self.respond(render_page(message))
            return
        if self.path == "/watchlist-add":
            message = add_to_watchlist(data.get("symbol", [""])[0])
            self.respond(render_page(message))
            return
        if self.path == "/watchlist-remove":
            message = remove_from_watchlist(data.get("symbol", [""])[0])
            self.respond(render_page(message))
            return
        if self.path == "/recommend-refresh":
            refresh_recommendations()
            self.respond(render_page("推薦排行已刷新。"))
            return
        if self.path == "/llm-ask":
            symbol = data.get("symbol", ["AAPL"])[0]
            question = data.get("question", ["這隻股票現在應該買入還是賣出？"])[0]
            self.respond(render_page(ask_stock_assistant(symbol, question)))
            return
        if self.path == "/auto-start":
            watchlist = data.get("watchlist", [",".join(load_watchlist())])[0]
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

# StockQuan

Python 股票量化 MVP，包含：

- Streamlit 界面
- 行情數據抓取與本地緩存
- RSS 新聞爬蟲
- 均線策略訊號
- Paper trading
- 下單前用戶確認隊列
- 可擴展券商 adapter

## 安裝

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

## 運行

零依賴本地版本，可直接使用 macOS 內置 Python：

```bash
PORT=8502 python3 web_app.py
```

打開：

```text
http://127.0.0.1:8502
```

功能包含：

- Yahoo Finance chart API 行情抓取
- 行情 CSV/JSON 本地緩存
- 策略訊號：價格、20/50 日均線、5 日動能
- 手動掃描並生成待確認訂單
- Watchlist：用戶可自行新增、移除、掃描股票
- Recommend List：系統按策略分數自動排序推薦股票
- Watchlist 自動監控，定時生成待確認訂單
- LLM 股票分析助手：可詢問特定股票，系統用趨勢跟隨、動量、均值回歸、多因子、風控框架判斷買入/持有/賣出
- 用戶確認後才 paper trading 成交
- 拒絕訂單
- Paper cash、持倉、成交記錄
- RSS 新聞爬蟲與緩存

Streamlit 版本需要安裝依賴：

```bash
streamlit run app.py
```

### 啟用 LLM

不設 API key 時，LLM 股票分析助手會使用本地多理論分析。若要接入 OpenAI：

```bash
export OPENAI_API_KEY="你的 OpenAI API key"
export OPENAI_MODEL="gpt-4o-mini"
PORT=8502 python3 web_app.py
```

LLM 只會基於系統抓到的行情和本地理論判斷生成分析，不會直接下單。

## 安全設計

此專案預設只啟用 paper trading。策略只會產生「待確認訂單」，必須由用戶在界面確認後才會執行。

如需接入真實券商，請新增 `Broker` adapter，並保留以下限制：

- `TRADING_MODE=paper` 以外的模式需要顯式配置
- 每筆訂單都要進入確認隊列
- 執行前檢查 `MAX_ORDER_NOTIONAL`
- 記錄所有訂單與狀態變更

## 目錄

```text
app.py                    Streamlit 界面
src/stockquan/config.py   配置
src/stockquan/data.py     行情數據
src/stockquan/news.py     新聞爬蟲
src/stockquan/strategy.py 策略
src/stockquan/broker.py   Paper broker 與 broker 介面
src/stockquan/orders.py   訂單確認隊列
src/stockquan/storage.py  本地 JSON/CSV 存儲
tests/                    基礎測試
```

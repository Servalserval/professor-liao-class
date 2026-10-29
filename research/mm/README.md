# 掛單研究腳本

`docs/market-making.md` 裡每一個數字都是用這些腳本算出來的。這裡放的是研究過程的原始腳本，
所以寫法比 `pmopt/mm` 粗糙：都在**這個資料夾裡**執行、讀寫同一個資料夾的 CSV（已經加進 .gitignore，重跑會重新產生）。
整理好的結果放在 `results/market_making_2026-10/`。

依序執行（都需要網路；第 3、4 步要抓 100 個市場一週的成交與價格，大約 10–20 分鐘）：

| 步驟 | 腳本 | 產生 | 內容 |
|---|---|---|---|
| 1 | `probe_rewards_all.py` | `rewards_all.csv` | 全站有流動性獎勵的市場、每日獎金、最大合格價差 |
| 2 | `reward_mm.py` | `reward_mm.csv`、`reward_mm_raw.pkl` | 獎金最高的市場：訂單簿快照、競爭者分數、掛不同距離時的預期份額 |
| 3 | `fetch_trades.py` | `trades/*.csv` | 這些市場過去一週的每一筆吃單成交（data-api /v2/trades） |
| 4 | `fetch_prices.py` | `prices/*.csv` | 同一段時間的 1 分鐘價格（/v2/prices-history，時間戳是秒） |
| 5 | `mm_backtest.py` | `mm_combo_*.csv` | 逐分鐘回測：每一條規則單獨加上去、兩個半週互相驗證；固定組合 A–F |
| 5b | `final_rules.py` | `mm_final_live_default.csv` | 文件裡那張規則表：基準、F0、F1、F1 − 波動暫停（實盤預設）、再拿掉單日停損 |
| 6 | `param_search.py` | `param_sensitivity.csv`、`param_random.csv` | 參數敏感度（一次改一個）與隨機搜尋（300 組），看前後半週的排名是否一致 |
| 7 | `universe.py` | `universe.csv` | 全站盤點：哪些市場現在能套用規則（價差、到期、中價範圍） |
| 8 | `sizing.py` | `universe_plan.csv` | 每個市場的最適本金：獎勵(C) − 門檻 × 預期虧損 × C |
| 9 | `plan_1m.py` | `plan_1m.csv`、`plan_1m_core.csv` | 投入 $1M 的配置，以及虧損率是估計值 1–4 倍時的報酬 |
| — | `reward_oos.py` | — | 早期的樣本外檢查（獎勵 vs 實際成交虧損） |
| — | `btc_maker_markout.py`、`btc_markout_short.py` | `btc_maker_trades.csv`、`btc_markouts.csv` | BTC 每日盤：掛單方抱到結算／短期的盈虧（沒有流動性獎勵，只有返佣） |

注意 `mm_backtest.py` 的回測沒有「看到未來」：影子帳與單日停損都只在 10 分鐘的結算時間過了之後才計入
（舊版的錯誤已修正，修正前後結果幾乎一樣）。

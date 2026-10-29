# 第四輪結果：掛單嚕流動性獎勵

說明與解讀見 [docs/market-making.md](../../docs/market-making.md)；產生這些檔案的腳本在 `research/mm/`。

| 檔案 | 內容 |
|---|---|
| `mm_final_live_default.csv` | 實盤預設規則（F1 − 波動暫停）的回測：每個市場、每個半週的每日獎勵、成交盈虧、返佣、掛單比例、淨額（$1,000／市場） |
| `mm_combo_F.csv`、`mm_combo_F1.csv` | 規則組合 F、F1 的同樣欄位（含獎勵最高的 5 個以色列市場；文件裡的數字排除它們） |
| `param_sensitivity.csv` | 從 F0 出發，一次改一個參數 |
| `param_random.csv` | 300 組隨機參數，兩個半週各自的淨額、虧損合計、最差市場、掛單比例 |
| `param_search_output.txt` | `param_search.py` 的完整輸出（敏感度、等級相關、交叉驗證、兩半都好的組合） |
| `plan_1m.csv` | 全部投入 $1M 時的每個市場配置（2026-10-29 的訂單簿） |
| `mm_plan_live.csv` | `python -m pmopt mm-plan --budget 350000` 在 2026-10-29 的輸出：999 個市場的本金、預期獎勵、預期淨額 |

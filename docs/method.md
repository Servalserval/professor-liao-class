# 方法說明

這份文件說明 `pmopt` 怎麼把期權價格換算成預測市場的機率，以及每一步的假設。

## 1. 兩邊的「合約規格」

先把兩個市場到底在賭什麼講清楚，這一步最容易被忽略。

| | Polymarket「above」盤 | Polymarket「hit」盤 | Deribit BTC 期權 |
|---|---|---|---|
| 例子 | Will the price of Bitcoin be above $80,000 on October 31? | Will Bitcoin dip to $77,500 in October? | BTC-31OCT26-80000-C |
| 看什麼價格 | Binance BTC/USDT | Binance BTC/USDT | Deribit BTC 指數（美元） |
| 看哪個時間點 | 指定日期中午 12:00 美東時間那根 1 分鐘 K 線的收盤價 | 整個月（美東時間）內任何一根 1 分鐘 K 線的最高價／最低價 | 到期日 08:00 UTC |
| 支付 | 是：$1，否：$0 | 是：$1，否：$0 | 現金結算 max(S−K, 0) |
| 對應的期權 | 歐式數位買權 | 觸價期權（one-touch） | — |

規則文字以各市場頁面的 Rules 為準；程式只從題目解析「題型與價位」，時間一律用 API 給的 `endDate`。

注意：
- **時間點不同**：Polymarket 的「10/31 中午 ET」是 16:00 UTC（夏令時間），Deribit 最近的兩個到期日是 10/31 08:00 與 11/1 08:00 UTC，所以要在兩個到期日之間內插。
- **價格來源不同**：Binance BTC/USDT 對 Deribit 的美元指數。如果 1 USDT 不等於 1 美元，履約價要換算，程式提供 `--usdt-usd` 參數。

## 2. 波動率曲面

Deribit 的 `public/get_book_summary_by_currency` 會回傳每個期權的 `mark_iv`（以百分比表示的隱含波動率）和 `underlying_price`（計算用的 forward）。

### 2.1 清理

- 同一個履約價有買權也有賣權時，優先用價外（OTM）那個。
- 只保留 |ln(K/F)| ≤ 0.35 的履約價。
- 沒有報價的深度價內／價外期權，交易所常給一個固定上限的 `mark_iv`。同一個數字出現在 3 個以上的履約價時，視為填充值丟掉。

### 2.2 SVI 擬合

每個到期日的微笑曲線用 SVI 參數化擬合（以總變異數 w = σ²T 表示）：

$$w(k) = a + b\left(\rho (k - m) + \sqrt{(k-m)^2 + s^2}\right), \quad k = \ln(K/F)$$

為什麼不直接逐點內插？最早的版本用 PCHIP 逐點內插、資料範圍外平坦外插，結果在資料邊界附近，
「收盤高於 K 的機率」會隨 K 增加而**上升**——等於機率密度是負的，數學上不可能。
SVI 的兩翼是平滑、線性延伸的，可以避免這個問題（`tests/test_surface.py::test_real_snapshot_gives_monotone_probabilities`）。

### 2.3 時間內插

任意時間 T 的總變異數，在相鄰兩個到期日之間對 T 線性內插：

$$w(k, T) = w_1(k) + \big(w_2(k) - w_1(k)\big)\frac{T - T_1}{T_2 - T_1}$$

總變異數隨時間不應下降（否則存在日曆套利）；資料不合理時取累積最大值。

## 3. 到期價格盤：數位買權

「T 時刻價格高於 K」的機率，等於數位買權的價格（不考慮折現，因為只有幾天）：

$$D(K) = -\frac{\partial C(K)}{\partial K}$$

如果波動率是平的，D(K) = N(d₂)。但實際上 σ 隨 K 變化，C(K) = BS(K, σ(K))，所以

$$D(K) = N(d_2) - \text{vega}(K)\cdot\frac{\partial \sigma}{\partial K}$$

程式用中央差分直接計算 −∂C/∂K，skew 的影響就自動包含在內；測試會檢查它和解析式一致。

「低於 K」= 1 − D(K)；「介於 L 和 H」= D(L) − D(H)。

## 4. 觸價盤：one-touch

在對數常態、forward 測度（漂移 −σ²/2）下，期間 T 內至少碰到一次 B 的機率（反射原理）：

- 向上（B > S）：$P = N\left(\frac{-b - \sigma^2T/2}{\sigma\sqrt T}\right) + \frac{S}{B}\,N\left(\frac{-b + \sigma^2T/2}{\sigma\sqrt T}\right)$
- 向下（B < S）：$P = N\left(\frac{b + \sigma^2T/2}{\sigma\sqrt T}\right) + \frac{S}{B}\,N\left(\frac{b - \sigma^2T/2}{\sigma\sqrt T}\right)$

其中 b = ln(B/S)。直覺：觸價機率大約是「到期時超過 B」機率的兩倍。

近似的地方：
- 只用一個波動率（觸價價位、到期時間那一點），沒有完整考慮微笑曲線。比較精確的做法是局部波動率或隨機波動率模型下的 Monte Carlo。
- Polymarket 看 1 分鐘 K 線的最高／最低價，這裡當成連續監看。

測試裡用 Monte Carlo（加上布朗橋修正）驗證公式。

## 5. 比較與 edge

- `fair`：期權推算的機率，也就是 Yes 股的公允價格（美元）。
- `edge_buy_yes = fair − ask − fee(ask)`：用賣價買 Yes，每股期望賺多少。
- `edge_sell_yes = bid − fair − fee(bid)`：用買價賣 Yes（等同買 No），每股期望賺多少。
- 手續費模型（假設）：`fee(p) = c · p · (1 − p)`。Polymarket 說明費用在 50% 時最高、越接近 0 或 1 越低，形狀符合，但係數 c 請查各市場 API 的 `feeSchedule`。預設 c = 0。
- `pm_iv`：把 Polymarket 中價當答案，反推它隱含的平坦波動率。注意價外數位期權的價格對 σ 不是單調的，程式只在單調的那一段找解。

**edge 不是無風險利潤。** 它是「假設期權市場是對的」之下的期望值，而且還沒扣避險成本。

## 6. 中心價格對齊

如果兩份資料不同步，或一邊報價過時，所有市場會往同一個方向偏，看起來到處都有錯價。

`fit_center` 把期權曲面整體乘上一個倍數 f（forward 與現貨一起平移，微笑曲線形狀不變），
找出讓「到期價格盤」的公允值最接近 Polymarket 中價的 f（只用中價在 5%–95% 之間的市場）：

$$f^* = \arg\min_f \sum_i \big(\text{fair}_i(f) - \text{mid}_i\big)^2$$

對齊之後剩下的差異，主要反映**分布形狀**（波動率、尾部），比較不受時間差影響。

用即時資料收集時，每一筆都會記錄 `sync_gap_sec`（兩邊抓取時間差）和 `center_factor`。
如果 `center_factor` 長期明顯不等於 1，而且時間差很小，那才可能是 Polymarket 真的整體偏多或偏空。

## 7. 還沒做、但真正要交易時一定要做的事

- 用 CLOB 訂單簿的**掛單深度**算可成交量（`--with-depth` 只拿最佳價那一檔）。
- 避險：用 Deribit 價差組合靜態複製數位期權，或用永續合約做 delta 避險，並計入成本。
- 結算來源的基差：Binance BTC/USDT 1 分鐘 K 線 vs Deribit 指數。
- 資金成本：Polymarket 的部位要鎖到結算（有持倉獎勵可部分抵銷）。
- 回測：在多天、多個到期日上收集同步資料，看錯價是否持續、是否在結算前收斂。

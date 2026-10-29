# 研究紀錄：2026 年 10 月，加密貨幣還有哪些套利機會？

這份是課前用 AI 做的網路研究整理（2026-10-29），目的是找出一個值得寫成程式的方向。
數字都是當時查到的資料，會隨市場變化；請以來源原文為準。

## 第一步：先看基準線

被動的「delta 中性」收益，現在大概只剩這樣：

| 指標 | 當時水準 | 來源 |
|---|---|---|
| BTC 永續資金費率 | 約 0.00–0.01%（每 8 小時），中性 | [convextrade](https://convextrade.com/metrics/btc-funding) |
| 跨所 BTC 資金費率價差 | 10/8–10/15 一週，HL 約年化 5.5%、Lighter 約 2.6%、Aster 約 1.8% | [cipher-intelligence](https://cipher-intelligence.io/blog/lighter-dex-vs-aster-dex-fees-2026/) |
| CME 12 月期貨基差 | 10/19 約 0.86%，換算年化約 4.5–5% | [proveyouredge](https://proveyouredge.com/articles/bitcoin-futures-basis-cme-spot-cash-and-carry) |
| Ethena sUSDe | 約 4.7%，10/1 起停止 ENA 補貼 | [Aavescan](https://aavescan.com/rates/ethena-susde)、[cryptoticker](https://cryptoticker.io/en/ethena-usde-incentives-end/) |
| 美國三個月國庫券 | 約 3.9%（8 月底） | [MarketXLS](https://marketxls.com/indicators/3-month-treasury-bill-rate) |

**結論**：主流幣的現貨＋永續 carry 只比美債多 0–1.5%，卻要承擔交易所對手風險與爆倉風險。
還有利潤的地方，集中在市場結構有摩擦的地方。

## 第二步：哪些地方還有摩擦？

| 類型 | 現況 | 主要風險 |
|---|---|---|
| 股票永續（Hyperliquid HIP-3）資金費率 | 散戶 24 小時想加槓桿做多美股，長期付費率給空方 | 週末跳空、oracle 異常 |
| 跨所資金費率 + Pendle Boros 鎖固定利率 | altcoin 費率常錯位，Boros 可把浮動費率鎖成固定 | 容量小、交易所風險 |
| 預測市場跨平台 | 仍有 YES+NO < $1 的機會，但結算規則常不同 | 規則差異、資金鎖到結算、開戶資格 |
| 借貸市場事件型機會 | 例：4 月 KelpDAO 事件期間 Aave USDC 利用率卡 100% 數天 | 需要判斷壞帳 |
| 永續 DEX 點數對沖 | 不是純套利，是賭空投價值 | 規則改變、點數被收回 |
| 泡菜溢價 | 10 月只有約 0.4–1.4%，大部分是 USDT 本身的溢價 | 外國人進不去 |
| 以太坊主網 CEX-DEX 套利 | 三個搜尋者拿走約四分之三的利潤 | 新人沒機會 |

主要來源：[Messari（Aave 與 KelpDAO）](https://messari.io/report/aave-cracks-in-the-monolithic-thesis)、
[SigBTC（泡菜溢價）](https://sigbtc.pro/en/kimchi-premium)、
[Wu et al.（CEX-DEX 套利）](https://arxiv.org/abs/2507.13023v2)、
[Pendle Print #122（Boros）](https://pendlefi.substack.com/p/pendle-print-122)。

## 第三步：網路上其他人做過什麼

| 玩法 | 重點 | 現在還行嗎 |
|---|---|---|
| Polymarket 15 分鐘 BTC 漲跌盤延遲套利 | 有錢包從 313 美元滾到約 43.8 萬美元 | 機會窗口從 12.3 秒縮到 2.7 秒；8/7 起改用 Chainlink TWAP 結算。HFT 戰場 |
| **預測市場 vs 期權** | 論文發現 Polymarket BTC 價格盤比 Binance 期權推算的機率平均高 5.6–6.3 個百分點、比 Deribit 高約 11 個百分點，集中在尾部 | **比的是建模，不是速度** |
| 用 LLM 找互相牽連的市場 | 研究估計 2024/4–2025/4 套利者從 Polymarket 賺走約 4,000 萬美元，研究者用 LLM 抽取市場間的邏輯關係 | 機會零星但成本低 |
| 天氣盤 | 用公開預報、修正系統偏差、找優勢 | 小規模可行，多為自述 |
| 運動盤做市 | 拿博彩公司賠率當公允價，吃流動性獎勵 | 一位開源作者表示優勢已衰退、逆選擇造成虧損 |
| Pre-IPO 永續 | 多個場所同時報價同一個沒有現貨可避險的標的 | oracle 風險大、一次性事件 |
| 盤前永續對沖預售額度 | 巨鯨在 Hyperliquid 低槓桿放空，鎖定預期拿到的額度價值 | 看個案 |

主要來源：
[Medium（Polymarket 延遲套利）](https://medium.com/@weare1010/claude-ai-trading-bots-are-making-hundreds-of-thousands-on-polymarket-2840efb9f2cd)、
[polymtradebot（TWAP 結算）](https://polymtradebot.com/)、
[arXiv 2606.19517（期權 vs 預測市場）](https://arxiv.org/pdf/2606.19517)、
[FC'26（尾部高估）](https://fc26.ifca.ai/defi/papers/market-efficiency-prediction-markets.pdf)、
[podshopguy（實際用 IBIT 期權對沖）](https://podshopguy.substack.com/p/polymarket-overprices-volatility)、
[arXiv 2508.03474（組合套利）](https://arxiv.org/abs/2508.03474v1)、
[pm.wiki（運動盤做市的退役機器人）](https://pm.wiki/projects/polymm-a-polymarket-sports-market-making-arbitrage-bot-in-python)、
[The Block（PUMP 盤前對沖）](https://www.theblock.co/post/361975/pump-token-ico)。

也要注意雜訊：網路上大量「AI 機器人一週賺十萬」的截圖多半是行銷；
有人實測各種 Polymarket 機器人虧了 3,840 美元，其中一個 GitHub 上的「套利機器人」其實是盜錢包程式
（[Medium](https://prediction-market.medium.com/polymarket-ai-bot-review-2026-heres-what-actually-worked-efb8e5d75740)）。

## 第四步：為什麼選「預測市場 vs 期權」

1. **錯價是結構性的**：兩邊參與者和資金互相隔開，不會因為有人寫了更快的機器人就消失。
2. **比的是建模能力**：要處理觸價與歐式的換算、到期日內插、波動率微笑，門檻在數學，不在延遲。
3. **可以先只做研究**：讀資料不需要帳號，也不用下單，適合當課堂示範。

## 第五步：上實盤之前要回答的問題

1. 用**可成交價**（不是中價）算，錯價還在不在？
2. **結算對不對得上**？每個盤的結算來源與時點都不同。
3. 錯價會不會在**結算前收斂**？會的話可以中途平倉，資金效率高很多。
4. **尾部風險能不能對沖**？偏差多在低機率那側，沒對沖就是「贏 8¢、輸 92¢」的賭局。

這個 repo 的 `collect` 指令，就是為了回答第 1 和第 3 題而設計的。

## 第六步：第二輪（同一天稍晚，接續上一個對話）

提問只有一句：「你就沿著這個一路往下研究」。這一輪的執行環境一樣連不到 Polymarket、Deribit、Binance，
所以沒有新資料，只能把現有快照挖深，再把「需要網路才能回答」的問題做成工具。

### 重新看快照：一個上一輪沒注意到的現象

對齊中心價格後，Polymarket 的**觸價盤**和 Deribit 幾乎完全一致（反推波動率 39.2% 對 38.9%、47.5% 對 47.7%），
偏差只集中在**每日到期盤**。這讓問題從「Polymarket 和期權一不一致」變成「Polymarket 自己的兩組市場一不一致」。
後者有兩個好處：不用假設期權是對的；兩組市場的結算來源（Binance 1 分鐘 K 線）相同。

### 文獻與規則查證（網路搜尋）

- 沒找到比較 Polymarket「每日到期盤」和「每月觸價盤」一致性的研究或文章。
  找到的多半是單一市場的紀錄頁，或是像 [podshopguy](https://podshopguy.substack.com/p/polymarket-overprices-volatility)
  這樣的交易想法。
- 規則（第三方頁面轉述，交易前請看市場頁面的 Rules）：
  - 每日盤：美東中午 12:00 那根 Binance BTC/USDT 1 分鐘 K 線的收盤價高於履約價就是 Yes
    （例：[bitcoin-above-on-december-22](https://polymarket.com/event/bitcoin-above-on-december-22)）。
  - 觸價盤：窗口內任何一根 1 分鐘 K 線的最高價（向上）／最低價（向下）碰到價位就是 Yes
    （例：週盤 [will-bitcoin-reach-76k-march-2-8](https://polymarket.com/market/will-bitcoin-reach-76k-march-2-8)、
    [struct.to 的市場紀錄](https://explorer.struct.to/markets/will-bitcoin-reach-80k-may-25-31-2026)）。
    月盤的窗口據搜尋結果中的第三方轉述，是從市場建立開始到當月最後一天美東 23:59，沒能核對原文。
  - 這兩條規則就是第 8 節「無模型支配關係」的依據：收盤價一定介於同一根 K 線的最高價與最低價之間。
- 手續費：形狀（在 50% 最高、靠近 0 或 1 趨近於零）各來源一致，但加密貨幣盤的係數說法不一：
  搜尋結果裡的官方文件快照寫 fee rate 0.25、指數 2（最高有效費率 1.56%），但時間早於 3 月的改版；
  [Sacra](https://www.sacra.com/c/polymarket/) 寫新版是 0.07，另一篇分析寫最高有效費率 1.80%
  （[Pine Analytics](https://pineanalytics.substack.com/p/polymarket-fee-rollout)、
  [Action Network](https://www.actionnetwork.com/news/polymarket-fees-explained-how-polymarket-trading-fees-work-in-2026)）。
  官方頁面 [docs.polymarket.com/trading/fees](https://docs.polymarket.com/trading/fees) 在這個環境打不開，沒能核對。

### 第二輪的結論（都還是單一快照，不是研究結論）

1. 106 組無模型的關係全部守住，好幾組剛好等於 $1.000。
2. 有一組「近似套利」：觸及 ↓77,500 Yes + 高於 78,000 Yes 共 $0.960，兩腳都歸零的機率約 0.3%。
3. 但觸價盤隱含的中心價格（81,654）比到期盤（80,880）高 0.96%，兩組報價很可能不是同一時間抓的；
   用任何一個一致的中心價格算，這個組合都不便宜。**最可能的解釋是時間差。**
4. 形狀上，觸價盤吻合期權的微笑，每日到期盤的尾巴比較厚。
   這是一個可以用歷史資料檢驗的假說：「Polymarket 高估尾部」是否主要發生在每日到期盤？

### 下一步（需要網路）

1. `collect`：同一輪抓取兩組市場，看近似套利在**同步資料**裡還在不在、持續多久。
2. `backtest`：拿過去幾個月已結算的每日盤，看尾部價格和實際頻率差多少（以天為群組算標準誤）。
3. 同樣的回測也可以做在月觸價盤上，比較兩種題型的校準。

## 第七步：第三輪（網路打開之後）

### 用原始資料核對第二輪的二手資訊

| 項目 | 第二輪（網路搜尋、第三方轉述） | 第三輪（API 原始回應、官方文件） |
|---|---|---|
| 手續費 | 0.07？0.25、指數 2？1.80%？ | [官方文件](https://docs.polymarket.com/trading/fees)：`fee = C × feeRate × p × (1 − p)`，加密貨幣 feeRate = 0.07、maker 0；每個市場的 `feeSchedule` 欄位也是 `{rate: 0.07, exponent: 1, takerOnly: true}` |
| 每日盤規則 | 「中午 12:00 那根 K 線的收盤價」 | 原文：the Binance 1 minute candle for BTC/USDT **closing at 12:00** in the ET timezone (noon) ... final "Close" price **higher than** the price specified。實際結算用的是 12:00 開盤那根（見下） |
| 觸價盤規則 | 月盤窗口據稱從市場建立開始（沒能核對） | 原文：any Binance 1 minute candle for BTC/USDT **from the creation of this market** through 11:59 PM ET on the last day of the month ... final High price **equal to or greater than** ... Price action before this market's creation will not be considered |

### 結算 K 線：規則文字 vs 實際結算

規則文字「closing at 12:00」可以讀成「11:59 開盤、12:00 收盤」那根。用 2025-10-01 到 2026-10-28 的 4,266 個已結算市場重算：

| 用哪一根 | 不一致的市場 |
|---|---|
| 12:00 開盤（12:00:59 收盤） | 0 / 4,266 |
| 11:59 開盤（12:00:00 收盤） | 3 / 4,252 |

那 3 個市場（2025-12-18 的 88,000、2026-04-24 的 78,000、2026-09-23 的 84,000），履約價都剛好夾在兩根 K 線的收盤價之間，
結算結果每次都跟著 12:00 開盤那根。**交易前要知道：規則文字和實際做法可能不一樣，而且只有在價格非常接近履約價時才看得出來。**

### 同步資料與一年回測的結論

詳見 README 的「第三輪」。重點：

1. 快照的大錯價主要來自資料不同步；同步資料裡兩組市場的中心價格只差約 0.3%（快照是 0.96%）。
2. 無模型違反仍然存在，但只有 1 個 tick、可成交幾十股。
3. 觸價盤的偏差落在模型不確定性之內（兩種近似可差 2–7¢）。
4. 一年的每日盤：Polymarket 的 Brier score 和 DVOL 模型打平；冷門結果的價格是實際頻率的 1.4–2 倍（24–48 小時前），但每股不到 1¢。
5. 文獻說的「平均高好幾個百分點」在這份資料裡沒有重現。可能原因：期間不同（請對照論文的樣本期間，這裡沒有查證）、比較對象不同
   （論文比期權推算的機率，這裡比實際結果）、或市場變得更有效率。要釐清，得照論文的方法與期間重做。

# Final Holdout 与压力矩阵报告

日期：2026-10-31

状态：**HOLDOUT_EVALUATED；ONE-TIME；NO RETRY**

- Holdout：2025-01-24 .. 2026-09-23，404 个交易日
- 原始结果 SHA-256：`sha256:40a2ae5c19150edc4c6d200bf3d473754a775c7a39e6ab2067c3d515d7bc9677`
- 预注册哈希：`sha256:391e3e140ad9b55821590b4296d708a6ad8b9c663a7cd05c674118588f9d9677`
- 72 个“杠杆 × 资金 × 费用”场景单元；每单元包含 G1-G6 多 seed 路径。

## 主比较

主比较单元为 50x、100 万、base。允许 G6 不优于 G4。

| Endpoint | Mean G4 | Mean G6 | G6-G4 | Holm p |
| :--- | :--- | :--- | :--- | :--- |
| first_liquidation_probability | 0.400 | 0.400 | 0.000 | 1.000 |
| time_to_first_liquidation_trading_days | 255.800 | 255.800 | 0.000 | 1.000 |
| expected_shortfall_95 | 0.879 | 0.879 | 0.000 | 1.000 |

结论：在该冻结样本外单元中，G4 与 G6 的强平概率、生存时间和 95% Expected Shortfall 没有可辨识差异；不能宣称 CVaR reward shaping 改善尾部风险。

## 1x 消融对照

G4/G6 在本表中使用五个 seed 的重新聚合；原始文件保持不变。

| Group | Paths | Return | Max DD | Sharpe | Fee drag | Liq. prob |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| G1 | 1 | 0.244 | -0.460 | 0.520 | 0.096 | 0.000 |
| G2 | 5 | 0.448 | -0.180 | 0.962 | -0.072 | 0.000 |
| G3 | 5 | 0.586 | -0.137 | 1.391 | -0.031 | 0.000 |
| G4 | 5 | 0.803 | -0.186 | 1.496 | -0.042 | 0.000 |
| G5 | 20 | 0.220 | -0.107 | 0.946 | -0.056 | 0.000 |
| G6 | 5 | 0.803 | -0.186 | 1.496 | -0.042 | 0.000 |

G1 月频与周频存在决策频率差异，不能把差异直接归因于模型能力。G5 的结果是 20 seed 分布均值。

## 图表

![1x 净值](C:/Users/think/Desktop/基于分层 AI 架构与极端杠杆压力测试的 ETF 量化回测系统/M2/reports/figures/holdout_g1_g6_1x_equity.png)

![强平热图](C:/Users/think/Desktop/基于分层 AI 架构与极端杠杆压力测试的 ETF 量化回测系统/M2/reports/figures/holdout_liquidation_heatmap.png)

![生存曲线](C:/Users/think/Desktop/基于分层 AI 架构与极端杠杆压力测试的 ETF 量化回测系统/M2/reports/figures/holdout_survival_g4_g6.png)

![资金费用](C:/Users/think/Desktop/基于分层 AI 架构与极端杠杆压力测试的 ETF 量化回测系统/M2/reports/figures/holdout_capital_fee_sensitivity.png)

![seed 分布](C:/Users/think/Desktop/基于分层 AI 架构与极端杠杆压力测试的 ETF 量化回测系统/M2/reports/figures/holdout_seed_distribution_1x.png)

## 重要限制与审计事件

- 强平与融资是 synthetic_v1 / synthetic_stress，不是任何券商事实；100x 仅是极端合成压力。
- 费用是参数化研究假设；交易与强平使用 raw OHLC，特征和标签使用点时化总收益。
- 结算遵守逐代码 T0/T1、ADV、停牌和涨跌停；未成交数量未被伪装成交。
- 首次强平后主路径终止；未强平路径右截尾。
- Holdout 执行期间发生两次实现故障：首次误读标签分区、第二次生存时间汇总类型错误。两次均在生成可用性能结论前失败；claim 未重置，之后仅从同一 claim 继续，并保留审计记录。最终原始结果哈希见上。
- 派生汇总修正了 G4/G6 原始 group 聚合只保留最后一个 seed 的问题；主比较与 seed 级原始明细未改。

Synthetic 100x leverage is an extreme stress scenario. Costs are parameterized assumptions. Values are not real broker financing capacity or guaranteed executable returns.

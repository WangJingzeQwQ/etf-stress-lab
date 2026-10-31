# 主对比预注册 v1

状态：**FROZEN_BEFORE_FINAL_HOLDOUT**

规范 JSON 哈希：`sha256:391e3e140ad9b55821590b4296d708a6ad8b9c663a7cd05c674118588f9d9677`

## 研究问题

Does CVaR reward shaping change high-leverage survival or tail outcomes relative to paired standard PPO?

该问题允许 G4 胜出、G6 胜出或没有可辨识差异。任何 CVaR 改善声明必须来自冻结后的 Holdout，并受配对 seed 与时间块 bootstrap 区间约束。

## 主比较单元

- 杠杆：50x（极端合成压力）
- 初始资金：¥1,000,000
- 费用：base
- 配对 seed：17, 29, 43, 71, 101
- 端点：first_liquidation_probability, time_to_first_liquidation_trading_days, expected_shortfall_95

## 冻结矩阵

- 杠杆：[1, 2, 5, 10, 25, 50, 75, 100]
- 资金：[100000, 1000000, 10000000]
- 费用情景：low, base, high
- G1 月频为主规则频率，周频为独立频率敏感性。
- G5 使用 20 个独立 seed；G2-G6 使用五组配对 seed。

## CVaR

- 主窗口：120 个交易日
- Alpha：0.1
- Lambda：0.5（仅 validation 选择）
- 损失定义为 `-return`；CVaR 为最差尾部正损失均值。
- 实现是 reward-shaped PPO，不是严格约束型 CVaR MDP。

## 生存与推断

- 事件为首次强平；未发生事件为右截尾。
- 使用 20 个交易日移动时间块 bootstrap，95%，2000 次。
- 主比较多端点采用 Holm 校正，α=0.05。
- 矩阵单元格不作为独立样本。

## 冻结承诺

在读取 Holdout 前冻结模型、scaler、seed、CVaR 参数、费用、杠杆、资金、指标和统计规则。Holdout 首次 claim 后不因结果修改阈值、seed、窗口或重试。

## 声明边界

Synthetic financing, synthetic_v1 liquidation and parameterized costs. No conclusion is a real broker financing capability.


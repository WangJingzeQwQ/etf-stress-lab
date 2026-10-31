# M0 实施判断

## 已理解的冻结口径

- 成交、现金、整手、手续费和强平只使用未复权 `raw_*`；qfq/provider 复权价格不得进入执行。
- 策略层输出 `TargetPortfolio(total_r, weights, signal_date, execution_date, data_cutoff, group, meta)`；不得包含资金、股数或目标市值。
- `total_r=0` 时权重为空；`total_r>0` 时非负权重之和必须等于 1，容差 `1e-8`。
- Broker 是现金、持仓、债务、费用、T+1、ADV、强平和账户状态的唯一权威。
- 债务采用事件式会计；价格变化不改债务，利息以昨日债务为基数。
- 主生存路径采用 `first_liquidation_terminates`；资不抵债终态永久禁止重入，外部补资只能创建独立路径。
- 合成强平线固定为 `max(maintenance_floor, maintenance_fraction/L_entry)`，默认 `0.0025` 和 `0.50`。
- 强平先计算毛回款，扣除卖出费用，再用净回款还债；记录 `bad_debt`。

## 数据状态与 M0 边界

当前没有真实 ETF 行情、历史成员、公司行动、交易规则或费用单据，AKShare 也未安装。M0 使用显式 `SYNTHETIC_FIXTURE` 和参数化费用验证工程机制，不形成真实市场结论。

本次只实现数据点视图、核心账本、Broker、合成 v1 强平、G1、确定性回放和测试；不实现 LSTM、PPO、G2-G6、144 矩阵、API 和 Vue。

## 规范冲突记录

未发现需要改变研究口径的实质冲突。两份文档均将合成强平、事件式债务、首次强平终止和 raw 成交定义为冻结规则；本次实现按项目简报执行，并将同一口径写入 `config.yaml`。


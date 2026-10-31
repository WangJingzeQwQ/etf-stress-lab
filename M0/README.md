# ETF 极端杠杆压力测试系统 — M0 垂直切片

本仓库当前只完成 M0 核心机制里程碑：点时化 ETF 数据视图、`TargetPortfolio`、事件式账户账本、Broker、T+1、100 股整手、raw 价格成交、佣金/最低佣金、滑点、ADV、停牌/涨跌停、合成 v1 强平、G1 月频动量与确定性 CLI 回放。

**重要边界：当前唯一端到端数据是 `SYNTHETIC_FIXTURE`。费用是参数化近似，强平和融资是合成压力规则，不是券商事实。输出收益、回撤和生存结果不能作为真实市场结论。**

## 已实现

- 策略层只输出 `TargetPortfolio`，Broker 按权重计算 `PV_target`、目标股数和整手偏差。
- 成交、估值和强平只读未复权 raw OHLC；provider qfq 字段被刻意保留但不参与执行。
- Decimal 账本记录现金、债务、持仓、佣金、其他费用、滑点、利息与强平结算事件。
- 每日顺序：T+1 解锁、基于前日债务计息、公司行动、raw open 重估与强平判断、调仓、raw close 快照、会计断言。
- 主动卖出受 T+1 限制；合成强平忽略 T+1 但仍受停牌、跌停和 ADV 约束。
- 资不抵债账户记录坏账后进入 `TERMINATED_INSOLVENT`，禁止零权益重入。
- G1 月频动量 top5 等权；候选不足时对全部候选等权，无候选时 `total_r=0`。
- 输出 trades、daily snapshot、metrics、target portfolio、audit events、config metadata 和 run manifest。
- 同配置重复运行使用确定性 result hash。

## 环境与安装

要求 Python 3.11 或更高版本。推荐从仓库根目录进入 M0 并创建独立环境：

```powershell
cd M0
chcp 65001 | Out-Null
$env:PYTHONUTF8 = "1"
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .
```

## 测试

```powershell
cd M0
chcp 65001 | Out-Null
$env:PYTHONUTF8 = "1"
.\.venv\Scripts\python.exe -m pytest -q
```

M0 实测结果：`40 passed`。

## 运行 G1 端到端回放

```powershell
cd M0
chcp 65001 | Out-Null
$env:PYTHONUTF8 = "1"
.\.venv\Scripts\python.exe -m backend.engine.run_backtest `
  --config config.yaml `
  --output reports/runs/m0_demo
```

输出目录包含：

| 文件 | 内容 |
| :--- | :--- |
| `trades.ndjson` | 逐笔成交、参考价、成交价、佣金、其他费用、滑点、现金流和未成交股数 |
| `daily_snapshot.ndjson` | 每日账户状态、现金、债务、持仓、权益、保证金率与实际杠杆 |
| `audit_events.ndjson` | 现金、债务、持仓、利息、费用和强平结算的逐事件账本 |
| `target_portfolio.ndjson` | 策略原始目标，不包含资金或股数 |
| `target_execution.ndjson` | Broker 按标的展开的 `PV_target/PV_actual/偏差/成交/未成交` |
| `metrics.json` | 收益、回撤、费用、换手、未成交和生存字段 |
| `config_metadata.json` | 冻结口径、数据来源、费率、融资、强平和执行模型元数据 |
| `run_manifest.json` | 计数、metadata、metrics 与确定性 result hash |
| `rejected_targets.ndjson` | 被整体拒绝的非法目标 |

金额以精确 Decimal 字符串写入 NDJSON，避免审计时因浮点显示产生账实不一致。

## 配置

主配置为 `config.yaml`，模板为 `config.example.yaml`。当前冻结项包括：

```yaml
financing_mode: synthetic_stress
capital_base_mode: initial_equity
liquidation_model: synthetic_v1
maintenance_fraction: 0.50
maintenance_floor: 0.0025
p0_1_frozen: true
reentry_policy: first_liquidation_terminates
```

费用、滑点、ADV 参与率和数据来源均显式配置。核心测试只在内存构造 fixture，不访问网络。

## 数据

数据可行性结论见 `reports/data_feasibility.md`。当前：

- 无真实 ETF OHLC、历史成员、退市覆盖或供应商缓存。
- AKShare 未安装，核心测试不依赖 AKShare。
- 端到端数据来自确定性 fixture，工作日历也是合成的。
- fixture 的 provider qfq 与 raw 价格显著不同，用于验证成交隔离。

## 已知限制

- 尚未实现 LSTM、PPO、G2-G6、144 组合矩阵、FastAPI 和 Vue 前端。
- 尚未实现监督学习标签、purge/holdout 切分和 scaler 冻结流程。
- 尚无真实历史成员、退市、公司行动、交易规则与费用单据。
- 只实现 `synthetic_stress` 和 `first_liquidation_terminates` 主路径。
- 费用配置当前是一条显式生效区间；多段历史费率查找尚未实现。
- G1 只实现月频版本，周频敏感性尚未实现。
- 指标是单路径指标；首次强平概率在当前输出中只是 0/1 标记，不构成频率估计。
- `fee_drag` 使用同信号、零佣金/零滑点但保留 ADV 与价格障碍的反事实路径。
- fixture 收益很高且波动路径不具现实代表性，不得据此评价 G1。

## 报告

- `reports/data_feasibility.md`
- `reports/implementation_assessment.md`
- `reports/m0_report.md`

下一里程碑应在核心测试继续通过后接入有版本哈希的真实点时化数据，再实现严格 purge/holdout 与 G2/G3；不得把 fixture 结果并入真实研究结论。


# M0 验收报告

日期：2026-10-29  
规范版本：`etf-stress-spec-2026-10-29-r2`  
结论：**M0 核心垂直切片完成；仅适用于合成 fixture 与参数化成本的机制验收。**

## 实现结果

- 已实现从 data view 到 Broker 再到 G1 CLI 的完整链路。
- 已实现点时化 universe/feature 截断一致性；特征不使用截止日之后的数据。
- 已实现 `TargetPortfolio` 严格校验、多标的权重分单和整手偏差。
- 已实现 Decimal 现金/债务账本及事件审计。
- 已实现 raw open 主动成交、raw low 合成强平、滑点价格边界和 ADV 部分成交。
- 已实现 T+1 主动卖出锁定、T+0 规则、最低佣金、停牌和涨跌停。
- 已实现事件式债务、前日债务计息、融资上限和按整手缩单。
- 已实现合成 v1 强平、先扣卖费再还债、坏账记录和终态账户。
- 已实现月频 G1 top5 等权、无候选空权重和确定性 result hash。
- 已生成逐笔交易、每日快照、审计事件、目标、指标、metadata 和 manifest。

## 实测命令与结果

```powershell
cd M0
$Python = "C:\Users\Public\TongYuan\.julia\miniforge3\python.exe"
& $Python -m pytest -q
```

结果：`40 passed in 0.87s`。

连续运行：

```powershell
& $Python -m backend.engine.run_backtest --config config.yaml --output reports/runs/determinism_a
& $Python -m backend.engine.run_backtest --config config.yaml --output reports/runs/determinism_b
```

两次结果均为：

```text
result_hash = sha256:a002c4386c7ccbf340fcb7d366c314a9d9bdd8f885c448cfaa2ea525cb44c411
trading snapshots = 195
trades = 32
G1 target portfolios = 8
```

两次运行的 trades、daily snapshot、target、target execution、audit events、metrics、manifest、config metadata 和 rejected targets 共 9 个文件均逐文件一致。

## 合成 fixture 示例指标

以下数字只用于证明输出链路完整，不能解释为真实 ETF 表现：

| 指标 | 值 |
| :--- | ---: |
| 总收益 | `0.4199544474684874` |
| 最大回撤 | `-0.13802578596725953` |
| 总佣金 | `1967.22` |
| 其他费用 | `0.00` |
| 滑点成本 | `3268.77` |
| 融资利息 | `17060.46253151255105233503680` |
| 最低佣金触发订单 | `4` |
| 未成交订单 | `1` |
| 账户终态 | `ACTIVE` |
| 首次强平 | 无 |
| `fee_drag` | `0.005308259955797812` |

## 数据可行性结论

当前无真实行情、历史成员、退市覆盖、公司行动、交易规则或费用单据；AKShare 未安装。M0 使用 `SYNTHETIC_FIXTURE`，工作日历和价格均是合成的。详细缺失项见 `reports/data_feasibility.md`。

## 规范冲突

未发现需要改变研究口径的实质冲突。实现按项目简报执行，并将冻结项同步写入 `config.yaml`。

## 已知限制

- 未实现 LSTM、PPO、G2-G6、144 矩阵、FastAPI 和 Vue。
- 未实现模型标签、purge/holdout 切分和 scaler 冻结流程。
- 真实历史数据尚未接入。
- 只实现 `synthetic_stress` 与 `first_liquidation_terminates`。
- 费用规则仅支持当前一条显式生效区间。
- G1 周频敏感性、保证金分数/利率/费用/ADV 全矩阵尚未运行。
- 单路径的首次强平概率只是 0/1 标记，不是统计概率。
- 多段费用规则、真实公司行动文件和跨供应商数据版本治理仍需后续实现。

## 建议下一里程碑

优先接入一小批具有版本哈希的真实 raw OHLC、历史 master、公司行动和逐代码交易规则，完成外部数据契约与截断测试；随后实现带 purge/embargo 的时间序列切分及 G2/G3 基线。模型和前端仍应等待真实数据与 Broker 机制回归测试稳定后再开始。


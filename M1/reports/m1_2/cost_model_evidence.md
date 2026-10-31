# M1.2 费用证据

当前费用表：

```text
data/m1_2/meta/cost_schedules.ndjson
```

包含 `low / base / high` 三个带生效区间的参数化情景。

## 证据等级

`source_kind=DATED_RESEARCH_ASSUMPTION`，`broker_specific=false`。

这意味着：

- 费用配置完整、日期明确、没有静默零值；
- 但它不是某个券商的历史对账单或佣金合同；
- 不能宣称复现真实券商成交成本；
- 研究报告必须标注 `cost_model=parameterized_etf_v1`。

ETF 主基准保持：

- 双边佣金率 3 bp；
- 每订单最低佣金 5 元；
- 单边滑点 5 bp；
- ETF 印花税 0；
- 交易所及其他费用为主基准显式 0，表示已包含在佣金假设中。


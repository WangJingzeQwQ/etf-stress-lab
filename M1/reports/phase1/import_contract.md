# Phase 1 真实数据导入契约

正式真实数据必须写入 `data/meta` 和 `data/raw/etf`，并关闭
`data_pipeline.formal_research` 之外的合成来源。

## 历史成员

`data/meta/etf_master.ndjson`，每行一个代码：

```json
{
  "code": "510300",
  "list_date": "2012-05-28",
  "delist_date": null,
  "name_history": [
    {
      "name": "沪深300ETF",
      "effective_from": "2012-05-28",
      "known_at": "2012-05-28"
    }
  ],
  "status_history": [
    {
      "status": "ACTIVE",
      "effective_from": "2012-05-28",
      "known_at": "2012-05-28"
    }
  ],
  "history_complete": true,
  "source": "AUDITED_HISTORICAL_MASTER"
}
```

状态只允许 `ACTIVE / SUSPENDED / DELISTED`。名称和状态必须记录实际可知时点，不能用今天的名称覆盖历史。

## 交易规则

`data/meta/trading_rules.ndjson`：

```json
{
  "code": "510300",
  "settlement": "T1",
  "effective_from": "2012-05-28",
  "effective_to": "2099-12-31",
  "limit_pct": "0.10",
  "known_at": "2012-05-28"
}
```

同一代码同一日期只允许一条有效规则。T+0/T+1 不得按品种推测。

## 公司行动

`data/meta/corporate_actions.ndjson`：

```json
{
  "code": "510300",
  "effective_date": "2024-02-01",
  "known_at": "2024-01-15",
  "action_type": "cash_dividend",
  "cash_per_share": "0.03",
  "split_ratio": "1"
}
```

拆分使用 `action_type=split` 并填写大于 0 的 `split_ratio`。`known_at` 不得晚于实际生效日。

## Raw 日线

`data/raw/etf/{code}.ndjson` 必须包含：

```json
{
  "code": "510300",
  "name": "沪深300ETF",
  "trading_date": "2024-01-02",
  "raw_open": "3.900",
  "raw_high": "3.950",
  "raw_low": "3.880",
  "raw_close": "3.930",
  "volume": "123456789",
  "amount": "480000000",
  "provider": "audited_provider",
  "adjustment": "none"
}
```

禁止把 qfq 价格写入 `raw_*`。复权序列必须由公司行动和点时化流程派生。

## 正式质量门

`data/meta/data_manifest.json` 必须满足：

- `source_kind=REAL_PROVIDER`；
- raw、历史成员、退市、公司行动、交易规则和费用覆盖全部为 true；
- `real_market_evidence_allowed=true`；
- 所有 master 记录的 `history_complete=true`；
- `data_pipeline.formal_research=true`。

任何缺失都会阻止回测，不以零费用、无限流动性或当前成员集代替。


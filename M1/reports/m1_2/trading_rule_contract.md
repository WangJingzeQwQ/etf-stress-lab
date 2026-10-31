# 逐代码交易规则证据契约

日期：2026-10-30  
目标文件：`data/m1_2/meta/trading_rules.ndjson`

## 1. 记录格式

每个 `code` 至少有一组覆盖其回测存续期的记录。允许一个代码有多行，但不得重叠。

```json
{
  "code": "510300",
  "settlement": "T1",
  "effective_from": "2018-01-01",
  "effective_to": "2026-10-29",
  "limit_pct": "0.10",
  "trading_status": "ACTIVE",
  "known_at": "2018-01-01",
  "source": "交易所或基金公告的明确名称",
  "evidence_id": "公告编号、交易所文件编号或可复核文档 ID",
  "evidence_sha256": "sha256:<64位十六进制>"
}
```

必填字段：

- `code`：六位 ETF 代码，必须存在于 observed master；
- `settlement`：仅允许 `T0` 或 `T1`；
- `effective_from` / `effective_to`：闭区间，ISO `YYYY-MM-DD`；
- `limit_pct`：小数比例，范围为 `(0, 1]`，例如 10% 写 `"0.10"`；
- `trading_status`：仅允许 `ACTIVE` 或 `SUSPENDED`；
- `known_at`：必须早于或等于 `effective_from`；
- `source`：真实证据来源，不接受 `unknown`、`TODO`、`synthetic` 等占位值；
- `evidence_id`：可用于复核的文档或记录 ID；
- `evidence_sha256`：可选；若提供，必须是完整 SHA-256。

## 2. 覆盖规则

对每个代码：

```text
required_effective_from = max(2018-01-01, list_date)
required_effective_to   = min(2026-10-29, delist_date)
```

除非 master 明确该代码不在回测区间内，否则上述闭区间必须由一条或多条规则连续覆盖。下一段的
`effective_from` 必须正好是上一段 `effective_to` 的后一日；任何缺口或重叠均阻止正式 bundle。

`SUSPENDED` 区间应使用独立的、有证据的规则行，不应把停牌日从规则表中省略。没有行情记录且规则
仍为 `ACTIVE` 的数据视为待核验缺口。

## 3. 审计与构建

生成缺口清单和机器可读审计：

```powershell
cd M1
$Python = ".\.venv\Scripts\python.exe"

& $Python -m backend.data_pipeline.trading_rule_audit `
  --config config.m1_2.yaml
```

输出：

```text
reports/m1_2/trading_rule_audit.json
reports/m1_2/trading_rule_gap.csv
```

证据完整后先做只读验证：

```powershell
& $Python -m backend.data_pipeline.build_formal_bundle `
  --config config.m1_2.yaml
```

全部检查通过后才写正式 bundle：

```powershell
& $Python -m backend.data_pipeline.build_formal_bundle `
  --config config.m1_2.yaml `
  --write
```

最后运行：

```powershell
& $Python -m backend.data_pipeline.formal_gate `
  --config config.m1_2.yaml
```

只有 `status=READY` 且 `formal_research_ready=true` 时才可把
`config.m1_2.yaml` 中的 `data_pipeline.formal_research` 改为 `true`。当前规则表为空，
1,810 个代码全部缺失，门禁必须保持 `BLOCKED`。

## 4. 禁止事项

- 不得按“股票/债券/跨境/商品”类别批量猜测历史规则；
- 不得给所有代码默认 `T1`；
- 不得用当前分类反推过去生效区间；
- 不得用供应商默认值或占位 `source/evidence_id` 通过门禁；
- 不得先生成 `source_kind=REAL_PROVIDER` manifest 再补证据。

## 5. 当前证据采集与正式规则的区别

`trading_rule_evidence.py` 已可从证监会基金电子披露站建立逐代码最新/基线
招募说明书索引，并提取 T+0/T+1 与涨跌幅原文。当前证据覆盖及缺口见
`trading_rule_evidence_status.md`。

证据索引、类别候选或 20% 候选 **不等于**正式规则行。只有在一致的产品分类、
适用交易所规则版本和明确生效区间三者闭合后，才能写入
`data/m1_2/meta/trading_rules.ndjson`。

# M1.2 交易规则证据获取状态

日期：2026-10-30  
结论：**54 项初始证据缺口已逐项复核；正式规则表和真实 bundle 已生成，formal gate=`READY`。**

## 1. 当前结果

- 历史 ETF 代码：1,810；
- 最新招募说明书文件索引：1,810 / 1,810；
- 2018 基线招募说明书文件索引：1,810 / 1,810；
- 当前可读文本：1,810 / 1,810；
- 基线可读文本：1,810 / 1,810；
- 公开基金公告索引：49,033 条；
- 证监会官方 PDF：3,288 份；
- 公开公告兜底 PDF：93 份。

两份原先缺文本的证据已用交易所等价文件补齐：

- `511230`：补充 2017 年上市交易公告书和 2019 年当前文本；
- `512210`：补充 2017 年更新招募说明书。

## 2. 54 项复核结论

初始 54 项缺口没有删除或改写成“通过”，而是保留在：

```text
reports/m1_2/trading_rule_evidence_gaps_initial_54.csv
```

逐项复核记录在：

```text
data/m1_2/cache/trading_rules/rule_evidence_resolutions.ndjson
```

处理结果：

| 原问题 | 数量 | 复核结果 |
| :--- | ---: | :--- |
| 规则变更公告待判定 | 37 | 逐项区分申赎结算、二级市场主动卖出和涨跌停字段；不把申赎结算公告自动视为 T0/T1 变化 |
| 2021 年前上交所跨境 ETF 区间 | 15 | 按产品招募说明书和当时有效交易所规则形成明确区间；2021 年申赎公告不自动改写二级市场规则 |
| 缺失当前文本 | 1 | `511230` 使用交易所等价披露文件补齐 |
| 缺失基线文本 | 1 | `512210` 使用交易所 2017 更新招募说明书补齐 |

机器可读覆盖报告：

```text
data/m1_2/cache/trading_rules/rule_evidence_coverage.json
```

当前字段：

```text
status=EVIDENCE_INDEXED
reviewed_issue_count=54
reviewed_resolutions=54
unresolved_issue_count=0
```

该覆盖报告本身只负责证据索引，不替代正式规则审计。正式规则由后续 builder 生成并通过独立门禁。

## 3. 正式规则表

正式文件：

```text
data/m1_2/meta/trading_rules.ndjson
```

结果：

```text
master codes:       1,810
rule rows:          3,333
complete codes:     1,810
incomplete codes:   0
overlap issues:     0
missing rule codes: 0
```

分类与规则计数：

```text
bond:         60
commodity:     3
crossborder: 262
gold:         16
money:         5
stock:      1,464

最终 T0 codes: 110
最终 T1 codes: 1,700
10% limit codes: 1,551
20% limit codes: 259
```

规则文件 SHA-256：

```text
sha256:9a6bc57cf769b90ad0d6690c91f1575c272cc75928d98b864150a86ee763e86e
```

证据引用审计解析 14,567 个引用，无法解析引用为 0。每一行都有连续生效区间、`known_at`、`source` 和 `evidence_id`。

## 4. 交易所依据

已归档在：

```text
data/m1_2/cache/trading_rules/official_rules/
```

包括：

- 上交所 2018 年交易规则通知及 `.doc`；
- 上交所 ETF 业务实施细则 2012 通知版、2020 年第一次和第二次修订；
- 上交所 2026 年交易规则；
- 上交所黄金 ETF、现金申赎债券 ETF 和科创板相关基金 20% 依据；
- 深交所基金交易细则 2016、2019 和 2020 版本链；
- 深交所 2019 年 340 号规则 PDF；
- 深交所 2020 年 730 号官方通知和官方 PDF；
- 创业板 20% 规则 FAQ 和 2020-08-24 首批注册制上市依据。

统一目录：

```text
official_rules/resolution_catalog.json
```

文件哈希：

```text
official_rules/files.sha256.json
```

## 5. 保留风险

深交所 2016 年规则官方附件 URL 在 2026-10-30 返回 404。当前保留公开镜像和明确状态：

```text
OFFICIAL_SOURCE_URL_RETURNED_404_ON_2026-10-30_MIRROR_RETAINED
```

不得把该镜像写成“已取得官方 PDF 原件”。后续若找到同内容官方附件，应补哈希并重新运行规则审计，但不能在未变更内容时静默替换证据状态。

另外，master 的上市/退市日期仍主要是交易所观察日或首个/最后有效 raw 报价精度；正式研究报告必须保留这一限制。

## 6. 可复核命令

```powershell
cd ".\M1"
& .\.venv\Scripts\python.exe -m backend.data_pipeline.trading_rule_audit `
  --config config.m1_2.yaml

& .\.venv\Scripts\python.exe -m backend.data_pipeline.formal_gate `
  --config config.m1_2.yaml
```

formal gate 当前返回码为 `0`，状态为 `READY`。

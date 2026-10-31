# M1.2 正式数据门状态

日期：2026-10-30  
结论：**`READY`，允许正式特征、标签和验证阶段研究。**

## 1. 当前结果

```text
status=READY
formal_research_ready=true
blocking_reasons=[]
```

七项检查全部通过：

| 检查项 | 结果 |
| :--- | :---: |
| raw 文件覆盖 | PASS，1,810 / 1,810 |
| historical master 覆盖 | PASS，1,810 / 1,810 |
| 公司行动 known_at | PASS，1,177 个事件 |
| 逐代码交易规则 | PASS，1,810 个代码、3,333 行 |
| raw 规则验证 | PASS，未解释价格带越界 0 |
| low/base/high 费用情景 | PASS，3 档 |
| 正式 bundle manifest 与重载 | PASS |

机器可读状态：

```text
reports/m1_2/formal_gate_status.json
```

## 2. 数据版本

```text
source_kind=REAL_PROVIDER
real_market_evidence_allowed=true
data_version=sha256:44127aa7dc2539e90375fa1e03b2b0f8dfafd21787dc18a105b6dd773f26f7c6
```

manifest：

```text
data/m1_2/meta/data_manifest.json
```

正式 bundle 构建报告：

```text
reports/m1_2/formal_bundle_build.json
```

构建器先执行规则、raw、公司行动、费用和 manifest 审计，再允许写入；写入后还会重新加载完整 bundle 并核对版本哈希。

## 3. raw 验证

```text
raw files:          1,810
raw rows:           1,435,802
market sessions:    2,139
price violations:   0
zero volume/amount: 1,507 rows / 380 codes
long gaps:          13 / 6 codes
missing sessions:   5,112
```

解释边界：

- 零成交行保留，但 Broker 视为不可成交；
- 缺失行情行不能当作可成交 bar；
- 长无报价间隔不能自动证明停牌，也不能插值或伪造成交；
- 两处拆股参考价异常 `510170`、`512120` 已用第二 raw provider 核验并记录覆盖依据。

## 4. 研究口径

- 成交、整手、现金和强平继续只使用 raw OHLC；
- 特征和标签使用带 `known_at/effective_date` 的点时化总收益；
- 费用只是 `parameterized_etf_v1` 研究假设，不是券商事实；
- 强平仍是 `synthetic_v1`，不代表真实券商保证金线；
- master 生命周期是观察日精度，不宣称官方上市/退市公告级完整；
- 深交所 2016 规则附件保留镜像风险说明。

## 5. 下游状态

`M1/config.m1_2.yaml` 与 `M2/config.m2_real.yaml` 的 `formal_research` 均为 `true`。

M2 已生成正式特征/标签快照和完整面板，完成多时点截断、退市、规则可知性及公司行动审计；Phase 3 已冻结按时间排序的 train / validation / holdout 边界。G2/G3 只使用 train 和 validation，final holdout 尚未评估。

# ETF 压力测试系统 — M1 / Phase 1 数据层

`M1` 继承并保留 `M0` 的核心垂直切片，同时按执行手册推进 Phase 1：

- 未复权 raw ETF 日线契约；
- 历史成员、已退市、暂停和更名区间；
- 请求间隔至少 3 秒、失败重试、失败队列和按代码断点续传；
- 逐代码带生效区间的 T+0/T+1、涨跌停规则；
- 带 `effective_date/known_at` 的公司行动；
- 仅用 T-1 及之前数据构建的动态 ETF 池；
- 点时化总收益价格，严格与执行用 raw OHLC 分离；
- 内容哈希、质量门和拒绝对 synthetic 数据发布真实研究结论。

**M1.1 已完成真实 raw 行情小批下载，M1.2 已完成 raw 覆盖、observed master、公司行动和费用情景；但官方生命周期证据、完整逐代码交易规则和正式 bundle 仍缺失，因此正式研究数据门未通过。** 当前 synthetic 回测只能用于机制、接口和回归测试，不能据此形成真实收益、爆仓概率或流动性结论。

## M1.1 与 M1.2

- `M1.1`：5 只代表 ETF 的真实 raw 日线小批验收，验证供应商、字段、限速、落盘和哈希；正式研究仍关闭。
- `M1.2`：按手册扩展到完整历史 ETF master、已退市与更名标的、全市场 raw、公司行动、交易规则和费用证据，并开启 `formal_research=true`。

M1.1 当前结果：

- provider：`akshare_sina_raw`；
- 5 个代码，每个 682 行；
- 覆盖 `2024-01-02` 至 `2026-10-29`；
- 全部 raw 记录 `adjustment=none`；
- `formal_research_allowed=false`。

报告：`reports/m1_1/small_batch_report.json`。

M1.2 已从交易所历史份额接口重建：

- 2018-01-01 至 2026-10-29；
- 106 个上交所月末快照；
- 18 个深交所半年区间；
- 1,810 个历史 ETF 代码；
- 119 个当前快照缺席的退市候选；
- 610 个观察到名称变化的代码。

该 master 是 provisional，全部 `history_complete=false`。全市场 raw 已落盘
1,810/1,810 个代码；`511680` 已由 BaoStock 补齐真实成交额，`159927`
供应商异常尾行已隔离。
公司行动候选已采集：1,177 个事件（910 分红、267 拆分）；其中 1,152 个
匹配公告日，25 个保留生效日保守回退。

```powershell
Get-Content -Raw data/m1_2/state/download_progress.json
Get-Content -Tail 20 reports/m1_2/logs/bulk_download.stderr.log
```

## 继承的 M0 能力

- `TargetPortfolio` 只包含策略目标，权重严格归一。
- Broker 是现金、债务、持仓、费用、T+1、ADV、强平和账户状态的唯一权威。
- 成交、整手和强平只使用 raw OHLC；qfq 仅供特征。
- Decimal 事件账本、最低佣金、滑点、停牌/涨跌停、ADV 部分成交。
- 合成 v1 强平和资不抵债永久终止。
- G1 月频 top5 动量、确定性 CLI 和完整审计输出。

## 环境

```powershell
cd M1
chcp 65001 | Out-Null
$env:PYTHONUTF8 = "1"
python --version
```

安装项目及可选 AKShare 数据提供器：

```powershell
& $Python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[data-provider]"
```

核心数据质量、动态池、复权因子和 Broker 测试均不需要网络。AKShare 只作为可选 adapter。

## 测试

```powershell
cd M1
.\.venv\Scripts\python.exe -m pytest -q
```

当前实测：`89 passed`，包含：

- M0 的 40 项 Broker、费用、T+1、ADV、强平和确定性回归；
- 历史更名与状态 `known_at`；
- 退市前可入池、退市后禁止入池；
- 动态池对未来数据截断不变；
- 规则缺失或时点未知时拒绝入池；
- raw frame 标准化和 `adjust=""` 约束；
- 请求间隔、重试、失败队列和断点续传；
- 公司行动复权因子时点一致性；
- synthetic 正式研究质量门；
- 交易所月度/半年成员快照重建与缓存续跑；
- 当前基金列表不能决定退市的回归测试。
- 历史 raw 长缺口和推断退市后的行情身份冲突审计。

## 生成并落盘 Phase 1 fixture

```powershell
cd M1
.\.venv\Scripts\python.exe -m backend.data_pipeline.materialize_fixture
```

生成：

```text
data/meta/etf_master.ndjson
data/meta/trading_rules.ndjson
data/meta/corporate_actions.ndjson
data/meta/cost_schedules.ndjson
data/meta/data_manifest.json
data/raw/etf/{code}.ndjson
reports/phase1/fixture_validation.json
```

该数据包包含七个 ETF-like 代码，其中一个明确退市、一个明确更名，并包含现金分红和拆分事件。它是合成数据。

## 构建动态池

```powershell
& $Python -m backend.data_pipeline.build_universe `
  --config config.yaml `
  --bundle-root data `
  --execution-date 2024-02-19 `
  --output reports/phase1
```

动态池审计文件记录每个代码的：

- `data_cutoff` 和 `execution_date`；
- 中文名称、上市日、退市日、状态；
- 历史条数、上市天数、ADV20；
- T+0/T+1 与涨跌停规则；
- 入池或拒绝原因；
- 来源数据版本和 universe hash。

## 构建点时化总收益

```powershell
& $Python -m backend.data_pipeline.build_total_return `
  --date 2024-03-04
```

输出 `data/derived/total_return_YYYYMMDD.ndjson/.json`。其中：

- `raw_open/raw_high/raw_low/raw_close` 保持未复权，可用于执行；
- `total_return_close` 只用于特征和收益；
- 只有 `known_at <= data_cutoff` 且已经生效的公司行动进入因子；
- 因子价格不得用于历史成交或整手股数。

## 真实数据接入

历史成员必须通过逐字段审计的 NDJSON 导入。AKShare 当前 spot 列表不能冒充历史成员和退市记录：

```powershell
& $Python -m backend.data_pipeline.fetch_master `
  --validate data/meta/etf_master.ndjson
```

正式 bundle 前必须运行逐代码交易规则审计：

```powershell
& $Python -m backend.data_pipeline.trading_rule_audit `
  --config config.m1_2.yaml
```

规则格式、日期覆盖和证据要求见
`reports/m1_2/trading_rule_contract.md`。审计会拒绝占位来源、时间缺口、区间重叠、
未来 `known_at`、未知代码和不完整生命周期。

真实规则证据采集进度、交易所规则版本和剩余核验项见：

```text
reports/m1_2/trading_rule_evidence_status.md
```

只有完整历史 master、退市、公司行动、交易规则、raw OHLC 和费用证据齐全时，`data_manifest.json` 才可标记：

```json
{
  "source_kind": "REAL_PROVIDER",
  "real_market_evidence_allowed": true
}
```

设置 `data_pipeline.formal_research: true` 后，任何缺失或不完整 coverage 都会阻止回测。当前配置必须是 `false`。

规则证据通过审计后，按以下顺序构建并复核正式 bundle：

```powershell
& $Python -m backend.data_pipeline.build_formal_bundle `
  --config config.m1_2.yaml

& $Python -m backend.data_pipeline.build_formal_bundle `
  --config config.m1_2.yaml `
  --write

& $Python -m backend.data_pipeline.formal_gate `
  --config config.m1_2.yaml
```

## 当前目录

```text
M1/
├── backend/data_pipeline/   # 数据契约、质量门、动态池、复权、供应商 adapter
├── backend/engine/          # 继承的 Broker、G1 和 CLI
├── data/raw/etf/            # raw NDJSON
├── data/meta/               # master、规则、公司行动、费用、manifest
├── data/derived/            # 点时化总收益
├── data/state/              # 下载状态与失败队列
├── reports/phase1/          # 数据质量与动态池审计
├── tests/                   # 89 项测试
└── config.yaml
```

## 已知限制

- 已获得 1,810 个交易所历史成员候选，但 list/delist 仍为推断值且 `history_complete=false`。
- raw 覆盖为 1,810/1,810；`511680` 已补齐，`159927` 异常尾部已隔离。
- 1,152 个公司行动事件已匹配公告日，25 个事件保留生效日回退；逐代码规则仍缺失，费用为明确的参数化研究假设。
- AKShare 已安装；当前列表接口仍不能单独证明历史成员完整性。
- 尚未实现 G2-G6、LSTM、PPO、144 矩阵、API 和 Vue。
- M2 已开始 Phase 2 特征/标签开发，但正式 Holdout、G2/G3 仍被数据门阻塞。
- 费用仍为参数化近似，融资与强平仍为冻结的合成压力模型。
- 合成 fixture 的收益和生存指标不是实证结果。

## 报告

- `reports/phase1/data_feasibility.md`
- `reports/phase1/phase1_report.md`
- `reports/phase1/import_contract.md`
- `reports/phase1/fixture_validation.json`
- `reports/runs/m1_phase1/`


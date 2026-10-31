# M1 / Phase 1 验收报告

日期：2026-10-30  
规范版本：`etf-stress-spec-2026-10-29-r2`

## 阶段判断

M1 按手册顺序进入 Phase 1 数据管线建设，没有跳到 G2-G6 或前端。当前状态：

- Phase 1 工程机制：通过；
- Phase 1 正式真实数据：阻塞；
- 允许使用：`SYNTHETIC_FIXTURE` 工程验收；
- 禁止使用：真实市场结论、真实爆仓概率或真实融资能力声明。

## 已实现

- `MasterRecord` 包含上市、退市、状态历史、名称历史和完整性标记。
- 名称和状态均以 `known_at + effective_date` 做点时选择。
- 动态池只读取 `data_cutoff=T-1` 的价格、成交额、状态和规则。
- 任一规则缺失、冲突或 T-1 时尚不可知，代码被排除并写入原因。
- 退市前样本可进入动态池，退市后排除。
- 点时化总收益只用截至 cutoff 已知且已生效的公司行动。
- raw OHLC、执行字段和时点化特征字段明确分离。
- 供应商下载具备 ≥3 秒限速、重试、失败队列和增量续传。
- 数据 bundle、动态池、时点化复权文件和回测结果均带内容哈希或数据版本。
- 正式研究模式拒绝 synthetic/parameterized 数据。
- M0 的 40 项 Broker/G1/强平/费用回归继续通过。
- M1.1 小批真实验收已从 AKShare 新浪 raw 接口取得 5 只 ETF，各 682 个交易日。
- volume 统一为股、amount 统一为人民币；当前名称仅作快照，不冒充历史成员。
- M1.2 已获得 1,810/1,810 个历史成员的 raw 文件，约 384.92 MiB。
- `511680` 已由 BaoStock 补齐 183 行真实成交额；`159927` 异常尾行已隔离。
- 公司行动分页采集已完成：1,177 个事件，其中分红 910、拆分 267；无硬失败。
- 1,152 个事件匹配到公告日，25 个事件使用生效日保守回退。

## 实测测试

```powershell
cd M1
$env:PYTHONDONTWRITEBYTECODE = "1"
$Python = "C:\Users\Public\TongYuan\.julia\miniforge3\python.exe"
& $Python -m pytest -q -p no:cacheprovider
```

结果：`83 passed in 4.25s`。

## 动态池人工样例

- `2024-02-09`：数据截止 `2024-02-08`，退市测试代码 `159999` 仍在池中。
- `2024-02-19`：数据截止 `2024-02-16`，`159999` 因已退市被排除。
- 更名 `510500` 在 `known_at=2023-11-15` 前不可见，在 `effective_date=2023-12-01` 后生效。
- 分红与拆分分别按 `known_at` 和 `effective_date` 进入点时化总收益。

## 确定性回放

`reports/runs/m1_determinism_a` 与 `reports/runs/m1_determinism_b` 两次独立运行结果一致：

```text
result_hash = sha256:d816e270cc59bc6e9b6ff66d99ab132a5f1a41c0ddb12a61be3c58d9843d748e
data_version = sha256:f0bae48a54b98021e25b3f1c901a9af760390c37a946f4e8a5b4e938941b4db6
trading days = 195
trades = 38
target portfolios = 8
identical output files = 9/9
```

这些收益与成交数只来自 synthetic fixture，不是真实市场结果。

## Phase 1 数据质量门

fixture 质量报告结果：

- `backtest_allowed=true`；
- `formal_research_allowed=false`；
- `error_count=0`；
- `warning_count=1`，警告为 synthetic 数据不得作为真实市场证据。

## 已知限制

- 已取得 observed-complete master、完整 raw、公司行动和参数化费用表，但逐代码交易规则仍缺失。
- AKShare 已安装，但当前列表接口本身不能满足历史成员完整性要求。
- 尚无真实数据供应商授权与版本记录。
- 费用仍为参数化近似，强平和融资仍为合成压力模型。
- M2 已开始 Phase 2 特征/标签工程开发，但正式 Holdout、G2/G3 仍被数据门阻塞。

## 下一阶段

必须先补齐逐代码历史 T+0/T+1、涨跌停和交易状态规则，并完成 observed master 的官方生命周期佐证。
正式 `REAL_PROVIDER` bundle 通过后，才能把 M2 的 development-only 特征和标签转为正式研究输入，再进入 Phase 3 切分与 G2/G3。


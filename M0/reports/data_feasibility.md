# ETF 数据可行性检查

检查日期：2026-10-29  
规范版本：`etf-stress-spec-2026-10-29-r2`  
M0 结论：**没有可用于正式研究结论的真实历史数据；继续使用明确标注的 `SYNTHETIC_FIXTURE` 验证引擎。**

## 环境检查

- Python：`3.11.4`，可执行文件为 `C:\Users\Public\TongYuan\.julia\miniforge3\python.exe`。
- 已安装：`pytest 9.0.2`、`pandas 2.3.3`、`numpy 1.26.4`、`PyYAML 6.0.3`。
- 未安装：`akshare`、`pyarrow`。
- 工作目录初始只有 `项目简报.md` 和 `manual for Codex.md`，没有 CSV、Parquet、数据库或供应商缓存。
- `backend/data_pipeline/akshare_adapter.py` 是可选网络适配器；核心测试不导入 AKShare，也不访问网络。

## 代表性检查范围

原计划抽查 `510300`、`510500`、`159915` 的 raw OHLC 和历史元数据。由于当前没有供应商依赖或本地数据，无法完成真实文件读取；以下六只代码仅存在于合成 fixture，不代表供应商数据已经验证：

| 代码 | fixture 中文名称 | 是否可用于 fixture 机制测试 | 是否可作为真实市场结论 |
| :--- | :--- | :---: | :---: |
| `510300` | 沪深300ETF | 是 | 否 |
| `510500` | 中证500ETF | 是 | 否 |
| `510050` | 上证50ETF | 是 | 否 |
| `159915` | 创业板ETF | 是 | 否 |
| `512100` | 中证1000ETF | 是 | 否 |
| `511010` | 国债ETF | 是 | 否 |

## 数据可用性矩阵

| 数据项 | 当前状态 | M0 处理 |
| :--- | :--- | :--- |
| 未复权 raw OHLCV | 真实数据缺失；fixture 字段完整 | 成交、整手、估值和强平只读 `raw_*` |
| 中文名称与代码 | 仅 fixture 映射 | 交易、快照和 target 全链路透传 |
| 历史成员、暂停与退市 | 未获得 | fixture 不声称覆盖幸存者/退市偏差 |
| 公司行动 | 未获得供应商事件 | 核心支持带 `effective_date/known_at` 的事件；fixture 未注入真实事件 |
| 复权因子 | 未获得真实因子 | 总收益字段必须带 `known_at/effective_date`；provider qfq 显式设置成不同值，禁止成交使用 |
| T+0/T+1 规则 | 未获得逐代码历史规则 | fixture 提供带生效区间的规则；缺失规则时零成交并记录 |
| 涨跌停规则 | 未获得真实规则表 | fixture 显式给出价格边界；越界视为不可成交 |
| ADV/成交量 | 未获得真实数据 | fixture 提供 T-1 ADV20 金额和参与率上限 |
| 佣金、最低佣金、其他费用 | 未获得点对点历史单据 | 使用显式 `parameterized_etf_v1`，不是任何券商事实 |
| 融资利率与授信 | 未获得 | 使用 `synthetic_stress` 参数；不是现实融资能力 |
| 强平规则 | 未获得券商协议 | 使用冻结的 `synthetic_v1`：`max(0.0025, 0.5/L_entry)` |

## Fixture 边界

`backend/data_pipeline/fixture.py` 使用工作日序列和固定随机种子生成 2023-10-02 至 2024-06-28 的六只 ETF-like 序列。它不是中国交易所日历，也不是实际 ETF 行情。fixture 的 `provider_qfq_close` 被故意设为与 raw 明显不同，以测试成交隔离。

每次结果均携带：

- `data_source_kind: SYNTHETIC_FIXTURE`
- `research_warning`
- `data_version`
- `financing_mode: synthetic_stress`
- `liquidation_model: synthetic_v1`
- `cost_model: parameterized_etf_v1`

因此 M0 结果只能用于工程、会计和机制验收，不能用于估计真实收益、真实爆仓概率或券商融资能力。

## 进入真实数据阶段前必须补齐

1. 经校验的逐代码 raw OHLCV，并保存供应商版本与内容哈希。
2. 包含已退市、暂停、更名区间的历史 `etf_master`。
3. 带 `effective_date/known_at` 的分红、拆分和总收益因子事件。
4. 逐代码、带生效区间的 T+0/T+1、涨跌停和交易状态规则。
5. 带生效日期的佣金、最低佣金、其他费用和滑点参数依据。
6. 历史 ADV 与成交量完整性检查，禁止缺失时默认为无限流动性。
7. 若报告 `capped_broker`，需点对点授信、融资和保证金规则；不得复用 `synthetic_stress` 结果。


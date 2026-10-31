# M1.2 数据准备状态

日期：2026-10-30  
当前状态：**FORMAL_READY**

## 已完成

- 从交易所历史名单重建 1,810 个 ETF 代码；
- 1,810/1,810 个 raw 文件落盘，硬错误 0；
- 1,177 个公司行动完成 `effective_date/known_at`；
- 1,810/1,810 个代码完成连续交易规则生命周期；
- raw 行情对涨跌停、零成交、缺行和长间隔完成审计；
- low/base/high 三档参数化费用情景已落盘；
- 正式 bundle 已写入并重新加载验证；
- formal gate 七项检查全部通过。

## 正式产物

```text
data/m1_2/meta/etf_master.ndjson
data/m1_2/meta/trading_rules.ndjson
data/m1_2/meta/corporate_actions.ndjson
data/m1_2/meta/data_manifest.json
```

数据版本：

```text
sha256:44127aa7dc2539e90375fa1e03b2b0f8dfafd21787dc18a105b6dd773f26f7c6
```

规则表 SHA-256：

```text
sha256:9a6bc57cf769b90ad0d6690c91f1575c272cc75928d98b864150a86ee763e86e
```

## 保留风险

- master 边界仍是观察日精度；
- 深交所 2016 规则官方 URL 404，公开镜像保留并标记；
- 费用不是券商事实；
- 合成强平不等同于真实券商规则；
- 缺失报价不是强行识别为停牌。

## 下游

M2 已启用正式 bundle，并完成正式特征/标签、截断、退市、规则可知性和公司行动回归。Phase 3 与 G2/G3 的验证阶段状态见 `M2/reports/`；final holdout 尚未评估。

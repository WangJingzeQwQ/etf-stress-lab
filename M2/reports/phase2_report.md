# M2 / Phase 2 工程报告

日期：2026-10-30  
状态：**正式真实数据路径 READY**

## 已实现

- `feature_v1`：14 个只读 `data_cutoff=T-1` 及以前数据的 ETF 截面特征；
- H=20 日 raw open 到 raw open 的时点化总收益标签；
- 同日截面中位数超额收益；
- 截面超额收益前 20% 分类；
- 完整历史特征/标签面板与单日快照；
- artifact SHA-256、语义 row hash 和 data version manifest；
- 多历史时点截断不变性；
- 退市后排除、规则 T-1 可知性和公司行动收益链审计。

## 正式产物

```text
data/features/real/features_20240131.ndjson
data/features/real/features_panel.ndjson
data/labels/real/labels_H20_20240201.ndjson
data/labels/real/labels_H20_panel.ndjson
```

规模：

```text
snapshot feature rows: 333
snapshot label rows:   333
panel feature rows:    553,457
panel label rows:      553,254
panel dates:           2,058
```

正式审计：

```text
reports/phase2_formal_validation.json
status=READY
checks=9
```

九个检查包括：

1. 单日 feature manifest/hash；
2. 单日 label manifest/hash；
3. 完整 feature panel；
4. 完整 label panel；
5. 三个历史切点的多截断一致性；
6. T-1 规则可知性；
7. 119 个退市代码列表外排除；
8. 分红/拆分收益链；
9. modeling dataset 分区与 Holdout 未评估标记。

## 关键边界

- 特征只读取截至 cutoff 的数据和已可知公司行动；
- 标签允许使用窗口内未来信息，是因为标签定义本身是已实现回归目标；
- 模型层不得读取 Holdout 标签；
- 603 条标签因不足 61 个特征历史 session 未进入 modeling panel，报告明确计数；
- 费用、成交可执行性和强平压力尚未在该 supervised layer 中落地。

## 测试

```text
M2: 107 passed
```

## 历史性能修复

时点化总收益和全历史特征构建已从逐 bar 扫描全部公司行动改为按代码索引/反向累计因子：

```text
old: O(bars × actions)
new: O(bars + actions)
```

因此完整 553k 行真实特征面板可在当前环境构建，而不是在十亿级循环中退化。

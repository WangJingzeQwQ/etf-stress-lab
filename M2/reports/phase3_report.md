# M2 / Phase 3 严格切分报告

日期：2026-10-30  
状态：**FROZEN；final holdout 未评估**

## 切分原则

- 使用时间顺序切分，不使用随机切分生成最终测试集；
- 主标签 H=20，purge 为 `H+1=21` 个交易日；
- 训练标签结束日早于 validation 开始；
- validation 标签结束日早于 holdout 开始；
- 冻结计划写入后不能静默改变边界；
- final holdout 通过独立 claim 文件限制为一次评估。

## 冻结边界

```text
train:      2018-04-03 .. 2023-08-23
validation: 2023-09-22 .. 2024-12-24
holdout:    2025-01-24 .. 2026-09-23
purge:      21 sessions
```

切分计划：

```text
data/splits/final_holdout_v1.json
```

计划哈希：

```text
sha256:bde8e6ce4ece98ddbf13a88096f2979210552ddbd1b2f43f7417455443228ced
```

## 泄漏审计

```text
train_label_end_before_validation_start=true
validation_label_end_before_holdout_start=true
holdout_starts_after_validation=true
partitions_are_disjoint=true
all_source_rows_assigned_once=true
purge_sessions_match_horizon_plus_one=true
```

完整机器报告：

```text
reports/phase3_split_manifest.json
```

## 扩展窗口

在 development 区间生成了 21 个扩展窗口 fold，每个 fold：

- 训练起点固定；
- 训练终点随 fold 向后扩展；
- validation 前至少保留 21 个 purge session；
- 不读取 final holdout。

## Holdout 状态

```text
holdout_evaluated=false
```

G2/G3 的 scaler、LightGBM 参数、LSTM 早期停止和指标都只使用 train/validation。final holdout claim 尚未创建。

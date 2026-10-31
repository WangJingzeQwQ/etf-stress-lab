# Validation 时间边界审计

日期：2026-10-31  
状态：**FIXED；Holdout 未受该边界影响**

## 问题

管理环境原先把最后一个决策日之后的完整 5 日持有块跑完。validation 的最后决策日贴近 `2024-12-24`，因此 302 个正式交易日被扩展成 305 个快照，额外日期属于 purge 区，不是 final Holdout。

## 修复

`PortfolioManagementEnv` 新增显式 `end_date`：

- 任何 step 的 `end_exclusive` 不得超过该日期；
- train 环境截止 `train_end`；
- validation 环境截止 `validation_end`；
- Holdout 环境截止 `holdout_end`；
- API validation replay 截止 `validation_end`。

## 影响审计

```text
validation expected sessions: 302
before fix snapshots:          305
after fix snapshots:           302
first session:                 2023-09-22
last session:                  2024-12-24
final Holdout affected:        no
models retrained:              no
preregistered artifact changed: no
```

Holdout 的结束日也是数据最后可用 session，最后持有块不能越过数据末端；因此冻结 Holdout 的 404 个交易日没有同类越界。

## 修正后的开发对照

```text
reports/validation_boundary_audit.json       # 1x
reports/validation_10x_boundary_audit.json   # 10x
```

这些文件是 Holdout 后的实现审计，不用于改变模型、阈值、seed、CVaR 参数、费用或主指标。


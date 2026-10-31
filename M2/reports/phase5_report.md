# M2 / Phase 5：G4-G6 配对验证报告

日期：2026-10-31  
状态：**VALIDATION_COMPLETE；Holdout 使用预注册后的独立结果**

## 1. 实现范围

- `PortfolioManagementEnv` 直接驱动统一 `Broker`，没有第二套现金、债务或费用账本；
- 动作严格为 `total_r ∈ {0, 0.25, 0.50, 0.75, 1.00}`；
- G4 使用 20 日滚动 Sharpe reward 的标准 PPO；
- G6 使用正损失口径、120 日窗口、`alpha=0.10` 的 CVaR reward shaping；
- G4/G6 使用相同 5 个 seed、相同网络、相同 4096 步训练预算；
- G5 使用 20 个独立 seed；
- 环境状态包含市场特征、`r_prev`、历史回撤、`debt/equity`、`margin_ratio` 和当前杠杆；
- 环境显式限制在 train/validation 的声明结束日内，不跨入 purge 或 Holdout。

## 2. CVaR 与配对预算

```text
CVaR selected lambda: 0.5
selection scope: validation only
selection seed: 17
candidate budget: 2048 steps per policy
main training budget: 4096 steps per policy
CVaR window: 120 sessions
CVaR alpha: 0.10
```

`loss_t=-return_t`，CVaR 是窗口内最差尾部正损失均值。G6 是 reward-shaped PPO，不是严格约束型 CVaR MDP。

## 3. Validation 10x 结果

Holdout 后的边界审计已把最后决策块严格截止到 `2024-12-24`，共 302 个交易日。

| 组别 | Paths | 平均累计收益 | 平均最大回撤 | 平均 Sharpe | 平均 95% ES | 首次强平概率 |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| G4 | 5 | -16.03% | -85.62% | -0.497 | +31.31% | 80% |
| G6 | 5 | -16.03% | -85.62% | -0.497 | +31.31% | 80% |

五个配对 seed 的路径逐项相同；validation 中未观察到 CVaR reward shaping 的增量。科学上允许该结果，不能围绕 G6 不改胜的结论调参。

## 4. G5

```text
seeds: 20
mean total return: 1.77%
median total return: -8.69%
5%..95% total return: -69.47% .. 110.61%
first liquidation probability: 50%
censored paths: 10
```

完整 seed 明细：

```text
reports/g5_seed_results.json
```

## 5. 产物

```text
data/models/phase5/g4_seed_{17,29,43,71,101}.pt
data/models/phase5/g6_seed_{17,29,43,71,101}.pt
reports/g4_g6_validation.json
reports/g5_seed_results.json
reports/g6_lambda_selection.json
reports/validation_boundary_audit.json
reports/validation_10x_boundary_audit.json
```

原始 `g4_g6_validation.json` 是预注册时冻结的 validation 记录，保留不改；Holdout 后新增的 `validation_*_boundary_audit.json` 仅修正环境期间边界，不参与 Holdout 模型选择。


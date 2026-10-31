# G2 / G3 验证报告

日期：2026-10-30  
状态：**VALIDATION_ONLY；final holdout 未评估**

## 数据与公平性

- 使用正式 `REAL_PROVIDER` bundle；
- 训练 186,814 行；
- validation 102,250 行；
- paired seeds：`17, 29, 43, 71, 101`；
- G2 与 G3 使用相同 14 个当前特征、相同 LightGBM 参数和配对 seed；
- G3 仅额外加入 20-session、16 维 LSTM 表征；
- G3 特征历史截止 validation 最后一个 `data_cutoff`；
- final holdout 未进入训练、scaler、早期停止或评估。

## Validation 聚合

| 指标 | G2 mean | G3 mean | G3-G2 95% paired interval |
| :--- | ---: | ---: | :---: |
| Mean rank IC | 0.0974 | 0.1020 | `[0.0030, 0.0062]` |
| Rank IC IR | 0.3482 | 0.3640 | `[0.0062, 0.0253]` |
| Top5 mean excess | -0.00077 | 0.00193 | `[0.00085, 0.00456]` |
| Top20 precision | 0.2342 | 0.2495 | `[-0.00354, 0.03427]` |

## 解释约束

- G3 的 validation 排名指标改善不等于最终策略胜出；
- Top20 precision 的配对区间跨 0；
- 区间是 seed 初始化差异，不是时间分块 bootstrap；
- 尚未叠加真实逐笔费用、T+1、ADV、杠杆、强平和账户终态；
- 不得用本表替代 final holdout 或 Phase 6 压力测试结论。

## 产物

```text
reports/g2_seed_results.json
reports/g3_seed_results.json
reports/g2_g3_paired_comparison.json
reports/g2_g3_paired_comparison.md
data/models/g2/
data/models/g3/
```

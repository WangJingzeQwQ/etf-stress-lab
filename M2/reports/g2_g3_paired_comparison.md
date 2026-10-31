# G2 vs G3 Paired Validation Comparison

Status: **VALIDATION_ONLY; final holdout not evaluated.**

Paired seeds: `[17, 29, 43, 71, 101]`

| Metric | G3 - G2 mean | 95% CI |
| :--- | ---: | :---: |
| `mean_rank_ic` | 0.00460873 | [0.00296855, 0.00624892] |
| `rank_ic_ir` | 0.0157792 | [0.00621647, 0.025342] |
| `mean_top5_excess_return` | 0.00270363 | [0.000847501, 0.00455976] |
| `top20_precision` | 0.0153642 | [-0.00354013, 0.0342686] |

The interval is across five paired initialization seeds. It is not a trading-result confidence interval and does not replace chronological block bootstrap or final holdout evidence.

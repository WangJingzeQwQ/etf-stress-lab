# ETF 压力测试系统 — M2（完整研究栈）

`M2` 承接已通过 formal gate 的 M1 真实 bundle，现已完成 C-A-B 模型、冻结 Holdout、压力矩阵、API 与 Vue 前端。

## 当前状态

```text
M1 formal gate: READY
M2 formal data audit: READY
Phase 3 split: FROZEN
G2/G3: VALIDATION_COMPLETE
G4/G5/G6: VALIDATION_COMPLETE
preregistration: FROZEN
final holdout: EVALUATED_ONCE
stress matrix: COMPLETE
API/frontend: COMPLETE
```

## 正式产物

```text
data/features/real/features_20240131.ndjson
data/features/real/features_panel.ndjson
data/labels/real/labels_H20_20240201.ndjson
data/labels/real/labels_H20_panel.ndjson
data/modeling/real/train.ndjson
data/modeling/real/validation.ndjson
data/modeling/real/holdout.ndjson
data/modeling/real/purged.ndjson
data/splits/final_holdout_v1.json
```

## 环境

```powershell
cd M2
$Python = "..\M1\.venv\Scripts\python.exe"
```

主要依赖：

```text
lightgbm>=4.0
torch>=2.0
fastapi>=0.110
uvicorn>=0.30
```

## 验证

```powershell
& $Python -m backend.features.validate_formal_data --config config.m2_real.yaml
& $Python -m pytest -q -p no:cacheprovider
```

当前：

```text
M2 formal checks: 9/9
tests: 119 passed
```

## 切分和模型

```powershell
& $Python -m backend.split.build_split_plan `
  --config config.m2_real.yaml `
  --labels data/labels/real/labels_H20_panel.ndjson

& $Python -m backend.models.build_modeling_dataset `
  --config config.m2_real.yaml

& $Python -m backend.models.layer_a --config config.m2_real.yaml
& $Python -m backend.models.layer_c --config config.m2_real.yaml
& $Python -m backend.models.compare_g2_g3
```

任何重跑都不能静默改变已冻结的 holdout 边界。final holdout 已通过 `claim_final_holdout` 一次性访问，claim 文件不可删除后重试。

## Phase 5 训练

```powershell
& $Python -m backend.models.run_phase5 --mode select-lambda --config config.m2_real.yaml
& $Python -m backend.models.run_phase5 --mode train --config config.m2_real.yaml
```

## API

```powershell
& $Python -m uvicorn backend.api.main:app --host 127.0.0.1 --port 8000
```

```text
GET /api/metadata
GET /api/matrix
GET /api/backtest?group=G4&leverage=1&initial_cash=1000000&cost_scenario=base
GET /api/etf/159796/daily
```

API 的 `/backtest` 使用 validation 数据做交互回放；Holdout 只读取冻结矩阵，不重跑。

## 前端

```powershell
cd frontend
npm install
npm run build
```

构建后的 `frontend/dist` 由 FastAPI 根路径托管。

## 报告

```text
reports/phase2_formal_validation.json
reports/phase2_report.md
reports/phase3_split_manifest.json
reports/phase3_report.md
reports/g2_seed_results.json
reports/g3_seed_results.json
reports/g2_g3_paired_comparison.json
reports/g2_g3_validation_report.md
reports/phase5_report.md
reports/validation_boundary_report.md
reports/preregistration/master_comparison_v1.md
reports/final_holdout_report.md
reports/holdout_analysis.json
```

G2/G3 的监督学习指标仍只属于 validation；最终策略结论必须引用预注册后的 Holdout 与合成压力边界。

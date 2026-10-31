# 复现指南

## 1. 环境要求

```text
Python >= 3.11
Node.js >= 20
Windows 10/11（当前一键启动脚本）
```

Linux/macOS 可以直接运行各 Python 模块和 `npm` 命令，但当前没有等价的 `.bat` 启动器。

## 2. 无需真实数据的核心演示

M0 使用确定性 synthetic fixture，适合在克隆仓库后快速验证 Broker 和执行协议。

```powershell
cd M0
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m backend.engine.run_backtest `
  --config config.yaml `
  --output reports/runs/m0_demo
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
```

输出中包含成交、每日快照、账户事件、目标组合、费用和执行审计。synthetic fixture 的结果不能当作真实市场结论。

## 3. 运行完整前端与 API

完整页面需要 M1 正式 bundle 和 M2 研究产物。GitHub 源码仓库不包含约 8.6 GB 的本地缓存与派生数据。

当前 Windows 环境：

```powershell
.\快速启动关闭.bat start
.\快速启动关闭.bat status
```

或者：

```powershell
$Python = "..\M1\.venv\Scripts\python.exe"
cd M2
& $Python -m uvicorn backend.api.main:app --host 127.0.0.1 --port 8000
```

首次构建前端：

```powershell
cd M2/frontend
npm ci
npm run build
```

页面地址：`http://127.0.0.1:8000/`。

## 4. 数据恢复

公开仓库保留小型 fixture、冻结切分配置和摘要报告，但不提交以下目录：

```text
M1/data/m1_1/
M1/data/m1_2/
M2/data/features/
M2/data/labels/
M2/data/modeling/
M2/data/models/
M2/data/splits/final_holdout_v1/
```

完整复现需要：

1. 通过 M1 数据管道重新采集并生成 formal bundle；
2. 构建 M2 特征和标签；
3. 恢复冻结的 split plan 与 Holdout claim；
4. 如只复现展示，恢复报告摘要和 API 所需的只读分析产物。

在没有正式 bundle 的机器上，`config.example.yaml` 和 fixture 用于机制验证，不应切换到 `formal_research=true`。

## 5. 测试矩阵

```text
M0: 40 tests
M1: 92 tests
M2: 119 tests
frontend production build: passed
KLine Chrome/CDP acceptance: passed
```

建议按阶段执行：

```powershell
cd M0
python -m pytest -q -p no:cacheprovider

cd ..\M1
python -m pytest -q -p no:cacheprovider

cd ..\M2
python -m pytest -q -p no:cacheprovider

cd frontend
npm run build
```

前端交互验收脚本位于 `scripts/kline_cdp_check.py`。它需要一个独立启动、带远程调试端口的本地 Chrome。

## 6. 一次性 Holdout 约束

final Holdout 已评估一次。不要为了整理仓库、修改 README 或调整前端而运行：

```text
M2/backend/engine/run_holdout.py
```

相关证据：

```text
M2/data/splits/final_holdout_v1.claim.json
M2/reports/holdout_evaluation_manifest.json
M2/reports/holdout_execution_incident.json
M2/reports/post_holdout_data_audit.json
```

如需再次研究，应创建新的研究问题、新的预注册和新的独立数据边界，不能覆盖或重用已消费 claim。


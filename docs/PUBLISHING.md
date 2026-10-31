# GitHub 发布清单

## 1. 推荐仓库信息

仓库名：

```text
etf-stress-lab
```

一句话描述：

```text
An auditable ETF leverage and tail-risk stress-testing lab with point-in-time data, ML/RL ablations, and a Vue research dashboard.
```

建议 topics：

```text
quantitative-research
machine-learning
reinforcement-learning
lightgbm
ppo
cvar
backtesting
etf
fastapi
vue3
ai-course-project
```

## 2. 首次上传前

- [ ] 确认根目录 `.gitignore` 生效；
- [ ] 确认 `git status` 中没有 `.venv`、`node_modules`、`.runtime` 或大型 NDJSON；
- [x] 原创代码与文档采用 MIT License；第三方行情数据不因代码许可证被一并授权；
- [ ] 确认 README 中的结果与研究冻结文档一致；
- [ ] 确认没有 API key、密码、Cookie、个人绝对路径；
- [ ] 修复或明确标注 M0/M1/M2 的阶段快照关系；
- [ ] 在 GitHub About 中填写简介、主页和 topics；
- [ ] 为仓库添加社交预览图，建议使用 `M2/reports/figures/dashboard_kline_replay.png`；
- [ ] 第一次推送后检查 GitHub 自动识别的主分支和语言比例。

## 3. 大文件策略

不要直接执行不带检查的：

```text
git add .
git commit ...
```

应先检查：

```powershell
git status --short
git diff --cached --stat
```

预计公开源码、测试、报告和图表远小于 100 MB。完整研究数据应通过独立归档交付，或在用户明确同意后使用 Git LFS/对象存储。

## 4. 推荐 Release 产物

第一个 release 可以包含：

```text
etf-stress-lab-v1.0.0-source.zip        GitHub 自动源码包
research-summary-v1.0.0.pdf             精简研究报告
research-evidence-v1.0.0.zip            报告、图表、冻结 split 与 claim
```

大型行情数据与训练权重不要混入源码 release。若需要分发，应单独审计授权、体积和内容哈希。

## 5. 许可证

原创代码与文档采用 MIT License，许可证文本见根目录 `LICENSE`。

行情数据、AKShare、交易所规则和第三方依赖分别受其自身条款约束，不能由代码许可证替代。本仓库不包含完整行情数据包。


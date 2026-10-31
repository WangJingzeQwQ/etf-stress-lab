# M1.2 公司行动采集报告

日期：2026-10-30  
状态：**PROVISIONAL**

## 结果

| 项目 | 数量 |
| :--- | ---: |
| 源分红记录 | 48,131 |
| 源拆分记录 | 1,621 |
| 匹配 ETF 公司行动 | 1,177 |
| 现金分红 | 910 |
| 拆分/份额折算 | 267 |
| 落在 provisional master 存续区间之外 | 95 |
| 网络失败 | 0 |

输出：

```text
data/m1_2/meta/corporate_actions.provisional.ndjson
reports/m1_2/corporate_actions_report.json
```

## known_at 口径

当前榜单没有公告披露日，因此所有事件使用：

```text
known_at = effective_date
```

这是保守时点，不会提前泄露未来事件，但不能替代公告级 `known_at`。

## 待核验

- 95 个事件因推断 list/delist 日期过粗而落在 provisional 窗口外；
- 拆分字段“拆分折算”需要与基金公告逐项核对份额比例；
- 同一代码可能发生基金身份变化，不能仅按六位代码关联全部历史事件。


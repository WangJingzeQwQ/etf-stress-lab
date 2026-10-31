# Phase 7 前端人工验收

验收日期：2026-10-31  
状态：**PASS — K 线显示与缩放 P0 已关闭**

## 根因

KLineCharts 9.8.12 会浅合并自定义 MA 线样式，并在折线合并阶段读取
`styles.dashedValue[0]`。原实现只给 MA 配置了 `color` 与 `size`，因此均线绘制时抛出：

```text
TypeError: Cannot read properties of undefined (reading '0')
```

异常发生在均线 `drawImp` 内，并中断同帧后续的成交覆盖层绘制。修复后 MA5/10/20/30
均提供完整的 `style / smooth / dashedValue` 参数。

同时关闭了以下会放大显示和缩放问题的实现：

- 删除超过 500 条时的索引抽样，保留连续 OHLC；
- 日线时间戳统一为 `Date.UTC(...)`，图表时区设为 UTC，避免日期偏移；
- 分离 OHLC 数据更新、视口恢复和 B/S/X 覆盖层重绘；
- 时间轴按交易快照日期裁剪，不再用 ETF 数据索引代替时间轴索引；
- ETF 请求增加防串写序号；切换标的时清空旧行情与旧覆盖层；
- `OnDataReady` 回调替代已弃用的 `applyNewData` 第三参数；
- `ResizeObserver` 对零尺寸与重复帧做保护。

## 自动化与浏览器验收

```text
frontend production build: PASS
Chrome: 156.0.8078.74
page: http://127.0.0.1:8000/
viewport: 1416 x 1045
Console/Log events after reload and all interactions: 0
```

验收脚本：

```powershell
C:\Users\Public\TongYuan\.julia\miniforge3\python.exe `
  scripts\kline_cdp_check.py --port 9224 --reload --full-check `
  --output M2\reports\figures\dashboard_kline_replay.png
```

检查结果：

| 项目 | 实测 | 结果 |
| :--- | :--- | :---: |
| 初始数据 | `159796`，302 bars，barSpace `8` | PASS |
| 缩小按钮 | barSpace `8 → 6.4` | PASS |
| 放大按钮 | barSpace `6.4 → 8` | PASS |
| 鼠标滚轮 | barSpace `8 → 8.8` | PASS |
| 拖动平移 | visibleRange.from `229 → 212` | PASS |
| 时间轴 | `2024-05-15`，151 bars，barSpace 保持 `8.8` | PASS |
| 窗口缩放 | chart width `664 → 853` | PASS |
| 零成交过滤 | `511360` 的 14 条未成交记录产生 0 个覆盖层 | PASS |
| 多笔成交 | `515230` 绘制 11 个 B/S 覆盖层 | PASS |
| ETF 切换 | 最终 `511220`，仅保留当前标的 B/S | PASS |
| 复位 | barSpace 回到 `8`，latest=`2024-12-24` | PASS |
| 画布尺寸 | `664 x 510`，与容器一致 | PASS |

最终调试值：

```json
{
  "activeCode": "511220",
  "size": { "width": 664, "height": 510 },
  "dataCount": 302,
  "visibleRange": { "from": 224, "to": 302, "realFrom": 226, "realTo": 305 },
  "barSpace": 8,
  "overlayCount": 2,
  "markerLabels": ["B", "S"],
  "firstDate": "2023-09-22",
  "lastDate": "2024-12-24"
}
```

## 功能清单

- [x] K 线为 raw OHLC，红涨绿跌，平盘灰色。
- [x] MA5/10/20/30 分别为白、金、粉、绿，且无绘制异常。
- [x] B/S/X 只使用 `shares > 0` 且 `execution_price > 0` 的真实成交。
- [x] 按钮、鼠标滚轮和拖动均能改变可见视口。
- [x] 时间轴更新保留缩放比例，K 线、成交和日期同步。
- [x] 视口宽度变化后画布自动适配，无拉伸或裁切。
- [x] 切换 ETF 不残留旧标的覆盖层。
- [x] x 轴日线日期无一日偏移。
- [x] 生产页面刷新及完整交互后 Console/Log 无事件。

最终截图：

```text
M2/reports/figures/dashboard_kline_replay.png
```

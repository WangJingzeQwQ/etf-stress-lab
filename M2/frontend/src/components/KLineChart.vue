<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ActionType, dispose, init } from 'klinecharts'

const props = defineProps({
  bars: { type: Array, default: () => [] },
  trades: { type: Array, default: () => [] },
  activeCode: { type: String, default: '' },
  cutoffDate: { type: String, default: '' },
})

const container = ref(null)
let chart = null
let overlayIds = []
let resizeObserver = null
let resizeFrame = 0
let dataFrame = 0
let markerFrame = 0
let applyingData = false
let dataQueued = false
let pendingDataFrame = null
let renderedCode = ''
let renderedCutoff = ''

const DEFAULT_BAR_SPACE = 8
const MA_COLORS = ['#FFFFFF', '#F6C85F', '#FF7EB6', '#3DDC84']

function dateTimestamp(dateText) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(dateText || ''))
  if (!match) return Number.NaN
  return Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]))
}

const activeBars = computed(() => {
  const cutoff = props.cutoffDate || props.bars.at(-1)?.date || ''
  const rows = []
  for (const bar of props.bars) {
    if (props.activeCode && bar.code !== props.activeCode) continue
    if (cutoff && bar.date > cutoff) continue

    const timestamp = dateTimestamp(bar.date)
    const open = Number(bar.open)
    const high = Number(bar.high)
    const low = Number(bar.low)
    const close = Number(bar.close)
    if (
      !Number.isFinite(timestamp) ||
      ![open, high, low, close].every(Number.isFinite) ||
      high < low
    ) {
      continue
    }

    const row = {
      timestamp,
      date: bar.date,
      open,
      high,
      low,
      close,
      volume: Number.isFinite(Number(bar.volume)) ? Math.max(0, Number(bar.volume)) : 0,
      turnover: Number.isFinite(Number(bar.amount)) ? Math.max(0, Number(bar.amount)) : 0,
    }
    const previous = rows.at(-1)
    if (previous?.timestamp === timestamp) rows[rows.length - 1] = row
    else if (!previous || previous.timestamp < timestamp) rows.push(row)
  }
  return rows.sort((left, right) => left.timestamp - right.timestamp)
})

const visibleTrades = computed(() => {
  const cutoff = props.cutoffDate
  return props.trades.filter(
    (trade) =>
      trade.code === props.activeCode &&
      Number(trade.shares) > 0 &&
      Number.isFinite(Number(trade.execution_price)) &&
      Number(trade.execution_price) > 0 &&
      Number.isFinite(dateTimestamp(trade.execution_date)) &&
      (!cutoff || trade.execution_date <= cutoff),
  )
})

function clearOverlays() {
  if (!chart) return
  for (const id of overlayIds) {
    try {
      chart.removeOverlay(id)
    } catch {
      // The chart may already be disposed during HMR or route teardown.
    }
  }
  overlayIds = []
}

function drawMarkers() {
  if (!chart || !chart.getDataList().length) return
  clearOverlays()
  for (const trade of visibleTrades.value) {
    const result = chart.createOverlay({
      name: 'simpleAnnotation',
      points: [
        {
          timestamp: dateTimestamp(trade.execution_date),
          value: Number(trade.execution_price),
        },
      ],
      lock: true,
      styles: {
        line: {
          color: trade.is_liquidation
            ? '#f3b544'
            : trade.side === 'buy'
              ? '#58d39b'
              : '#ef6b68',
          size: 1,
          style: 'dashed',
          dashedValue: [3, 2],
        },
        text: {
          color: '#ffffff',
          size: 11,
          style: 'fill',
          backgroundColor:
            trade.is_liquidation
              ? '#8f6514'
              : trade.side === 'buy'
                ? '#176846'
                : '#8f3634',
        },
      },
      extendData: trade.is_liquidation ? 'X' : trade.side === 'buy' ? 'B' : 'S',
    })
    if (result) overlayIds.push(result)
  }
}

function finishDataRender() {
  if (!chart || !pendingDataFrame) return
  const render = pendingDataFrame
  pendingDataFrame = null
  applyingData = false

  const barSpace = Number.isFinite(render.barSpace) ? render.barSpace : DEFAULT_BAR_SPACE
  chart.setBarSpace(render.resetView ? DEFAULT_BAR_SPACE : barSpace)
  chart.scrollToRealTime()
  renderedCode = render.code
  renderedCutoff = render.cutoff
  drawMarkers()

  if (dataQueued) {
    dataQueued = false
    renderData()
  }
}

function renderData() {
  if (!chart) return
  if (applyingData) {
    dataQueued = true
    return
  }

  applyingData = true
  clearOverlays()
  const rows = activeBars.value
  const resetView = props.activeCode !== renderedCode || !chart.getDataList().length
  const currentBarSpace = chart.getBarSpace()
  pendingDataFrame = {
    code: rows.length ? props.activeCode : '',
    cutoff: props.cutoffDate,
    resetView,
    barSpace:
      Number.isFinite(currentBarSpace) && currentBarSpace > 0
        ? currentBarSpace
        : DEFAULT_BAR_SPACE,
  }
  chart.applyNewData(rows)
}

function scheduleDataRender() {
  if (dataFrame) cancelAnimationFrame(dataFrame)
  dataFrame = requestAnimationFrame(() => {
    dataFrame = 0
    renderData()
  })
}

function scheduleMarkerRender() {
  if (markerFrame) cancelAnimationFrame(markerFrame)
  markerFrame = requestAnimationFrame(() => {
    markerFrame = 0
    if (
      chart &&
      renderedCode === props.activeCode &&
      renderedCutoff === props.cutoffDate
    ) {
      drawMarkers()
    }
  })
}

function zoomBy(factor) {
  if (!chart) return
  const current = chart.getBarSpace() || DEFAULT_BAR_SPACE
  chart.setBarSpace(Math.min(40, Math.max(2, current * factor)))
}

function resetView() {
  if (!chart) return
  chart.setBarSpace(DEFAULT_BAR_SPACE)
  chart.scrollToRealTime()
}

function getViewState() {
  if (!chart) return null
  return {
    activeCode: props.activeCode,
    cutoffDate: props.cutoffDate,
    size: chart.getSize(),
    dataCount: chart.getDataList().length,
    visibleRange: chart.getVisibleRange(),
    barSpace: chart.getBarSpace(),
    overlayCount: overlayIds.length,
    markerLabels: visibleTrades.value.map((trade) =>
      trade.is_liquidation ? 'X' : trade.side === 'buy' ? 'B' : 'S',
    ),
    firstDate: activeBars.value.at(0)?.date || '',
    lastDate: activeBars.value.at(-1)?.date || '',
  }
}

defineExpose({
  zoomIn: () => zoomBy(1.25),
  zoomOut: () => zoomBy(0.8),
  resetView,
  getViewState,
})

onMounted(async () => {
  await nextTick()
  chart = init(container.value, {
    timezone: 'UTC',
    styles: {
      grid: {
        horizontal: { color: 'rgba(120,145,175,.15)' },
        vertical: { color: 'rgba(120,145,175,.10)' },
      },
      candle: {
        type: 'candle_solid',
        bar: {
          upColor: '#ef4b4b',
          downColor: '#22b573',
          noChangeColor: '#8a96a8',
          upBorderColor: '#ef4b4b',
          downBorderColor: '#22b573',
          noChangeBorderColor: '#8a96a8',
          upWickColor: '#ef4b4b',
          downWickColor: '#22b573',
          noChangeWickColor: '#8a96a8',
        },
        priceMark: {
          last: {
            upColor: '#ef4b4b',
            downColor: '#22b573',
            noChangeColor: '#8a96a8',
            text: { color: '#eaf2fb' },
          },
        },
        tooltip: { text: { color: '#dbe5f1' } },
      },
      xAxis: { axisLine: { color: '#26374b' }, tickText: { color: '#8195ad' } },
      yAxis: { axisLine: { color: '#26374b' }, tickText: { color: '#8195ad' } },
    },
  })
  chart.createIndicator(
    {
      name: 'MA',
      calcParams: [5, 10, 20, 30],
      styles: {
        // KLineCharts shallow-merges this array. Every compared line property
        // must be present or its drawing loop throws on dashedValue[0].
        lines: MA_COLORS.map((color) => ({
          color,
          size: 1.4,
          style: 'solid',
          smooth: false,
          dashedValue: [2, 2],
        })),
      },
    },
    false,
    { id: 'candle_pane' },
  )
  chart.setZoomEnabled(true)
  chart.setScrollEnabled(true)
  chart.setPriceVolumePrecision(3, 0)
  chart.setOffsetRightDistance(16)
  chart.setLeftMinVisibleBarCount(10)
  chart.setRightMinVisibleBarCount(10)
  chart.subscribeAction(ActionType.OnDataReady, finishDataRender)

  resizeObserver = new ResizeObserver((entries) => {
    const box = entries[0]?.contentRect
    if (!chart || !box || box.width < 2 || box.height < 2) return
    if (resizeFrame) cancelAnimationFrame(resizeFrame)
    resizeFrame = requestAnimationFrame(() => {
      resizeFrame = 0
      chart?.resize()
    })
  })
  resizeObserver.observe(container.value)
  renderData()
})

watch(
  () => [props.bars, props.activeCode, props.cutoffDate],
  scheduleDataRender,
  { flush: 'post' },
)

watch(
  () => [props.trades, props.activeCode, props.cutoffDate],
  scheduleMarkerRender,
  { flush: 'post' },
)

onBeforeUnmount(() => {
  if (dataFrame) cancelAnimationFrame(dataFrame)
  if (markerFrame) cancelAnimationFrame(markerFrame)
  if (resizeFrame) cancelAnimationFrame(resizeFrame)
  resizeObserver?.disconnect()
  resizeObserver = null
  clearOverlays()
  if (chart) chart.unsubscribeAction(ActionType.OnDataReady, finishDataRender)
  if (chart && container.value) dispose(container.value)
  chart = null
})
</script>

<template>
  <div id="kline-container" ref="container"></div>
</template>

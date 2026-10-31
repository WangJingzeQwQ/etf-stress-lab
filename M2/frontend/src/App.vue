<script setup>
import { computed, onMounted, ref } from 'vue'
import { useDashboardStore } from './stores/dashboard'
import KLineChart from './components/KLineChart.vue'
import Timeline from './components/Timeline.vue'

const store = useDashboardStore()
const config = ref({
  group: 'G4',
  leverage: 1,
  initialCash: 1000000,
  costScenario: 'base',
  seed: 17,
})
const timelineIndex = ref(0)
const klineRef = ref(null)
const groups = ['G1', 'G2', 'G3', 'G4', 'G5', 'G6']
const leverages = [1, 2, 5, 10, 25, 50, 75, 100]
const cashSizes = [
  { label: '10 万', value: 100000 },
  { label: '100 万', value: 1000000 },
  { label: '1000 万', value: 10000000 },
]

const frozenMeta = computed(() => store.metadata || {})
const currentSnapshot = computed(() => {
  if (!store.snapshots.length) return null
  const index = Math.min(timelineIndex.value, store.snapshots.length - 1)
  return store.snapshots[index]
})
const metrics = computed(() => store.metrics || {})
const timelineMetrics = computed(() => {
  const snapshot = currentSnapshot.value
  if (!snapshot || !store.snapshots.length) {
    return {
      totalReturn: null,
      maxDrawdown: null,
      sharpe: null,
      firstLiquidationProbability: null,
      totalCommission: null,
      feeDrag: null,
      accountStatus: null,
    }
  }
  const index = Math.min(timelineIndex.value, store.snapshots.length - 1)
  const visible = store.snapshots.slice(0, index + 1)
  const initial =
    Number(store.result?.metadata?.initial_cash) ||
    Number(visible[0]?.equity) ||
    1
  const currentEquity = Number(snapshot.equity)
  const totalReturn = currentEquity / initial - 1

  let peak = initial
  let maxDrawdown = 0
  const dailyReturns = []
  let previous = initial
  for (const row of visible) {
    const equity = Number(row.equity)
    peak = Math.max(peak, equity)
    if (peak > 0) maxDrawdown = Math.min(maxDrawdown, equity / peak - 1)
    if (previous > 0) dailyReturns.push(equity / previous - 1)
    previous = equity
  }
  let sharpe = null
  if (dailyReturns.length >= 2) {
    const mean = dailyReturns.reduce((sum, value) => sum + value, 0) / dailyReturns.length
    const variance =
      dailyReturns.reduce((sum, value) => sum + (value - mean) ** 2, 0) /
      (dailyReturns.length - 1)
    const volatility = Math.sqrt(variance)
    sharpe = volatility > 1e-12 ? (mean / volatility) * Math.sqrt(252) : null
  }

  const currentDate = snapshot.trading_date
  const liquidationDate = metrics.value.first_liquidation_date
  const firstLiquidationProbability =
    liquidationDate && liquidationDate <= currentDate ? 1 : 0

  const counterfactualCurve =
    store.result?.counterfactual_equity_curve ||
    store.result?.fee_drag_counterfactual?.equity_curve ||
    []
  let counterfactualEquity = null
  for (const point of counterfactualCurve) {
    if (point.date > currentDate) break
    counterfactualEquity = Number(point.value)
  }
  const feeDrag =
    counterfactualEquity == null
      ? null
      : counterfactualEquity / initial - currentEquity / initial

  return {
    totalReturn,
    maxDrawdown,
    sharpe,
    firstLiquidationProbability,
    totalCommission:
      snapshot.cumulative_commission == null
        ? null
        : Number(snapshot.cumulative_commission),
    feeDrag,
    accountStatus: snapshot.account_status,
  }
})
const groupedCodes = computed(() => {
  if (!store.result) return []
  const names = new Map()
  store.bars.forEach((bar) => names.set(bar.code, bar.name))
  for (const snapshot of store.snapshots) {
    for (const position of snapshot.positions || []) {
      names.set(position.code, position.name)
    }
  }
  for (const trade of store.trades) names.set(trade.code, trade.name)
  return [...names.entries()]
    .map(([code, name]) => ({ code, name: name || code }))
    .sort((left, right) => left.code.localeCompare(right.code))
})

const baseMatrix = computed(() => {
  const cells = store.matrix?.cells || []
  return leverages.map((leverage) =>
    cells.find(
      (cell) =>
        Number(cell.leverage) === leverage &&
        Number(cell.initial_cash) === 1000000 &&
        cell.cost_scenario === 'base',
    ),
  )
})

function percent(value, digits = 1) {
  return value == null ? '—' : `${(Number(value) * 100).toFixed(digits)}%`
}
function money(value) {
  return value == null
    ? '—'
    : `¥${Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 0 })}`
}
function number(value, digits = 2) {
  return value == null ? '—' : Number(value).toFixed(digits)
}
function metricTone(value) {
  if (value == null) return ''
  return Number(value) >= 0 ? 'positive' : 'negative'
}
function costTone(value) {
  if (value == null) return ''
  return Number(value) > 0 ? 'negative' : 'positive'
}
function pickGroup(cell, group) {
  return cell?.groups?.[group] || {}
}
function selectMatrix(cell) {
  if (!cell) return
  config.value.leverage = Number(cell.leverage)
  config.value.initialCash = Number(cell.initial_cash)
  config.value.costScenario = cell.cost_scenario
}
function liquidCellStyle(cell, group) {
  const value = pickGroup(cell, group).first_liquidation_probability || 0
  const alpha = Math.min(0.94, 0.08 + Number(value) * 0.8)
  return {
    background: `rgba(239, 107, 104, ${alpha})`,
    color: value > 0.45 ? '#fff' : '#e9eef6',
  }
}
async function runReplay() {
  await store.runBacktest({ ...config.value })
  timelineIndex.value = Math.max(0, store.snapshots.length - 1)
}
async function chooseCode(code) {
  await store.loadBars(code)
}
function zoomKline(direction) {
  if (direction === 'in') klineRef.value?.zoomIn()
  else if (direction === 'out') klineRef.value?.zoomOut()
  else klineRef.value?.resetView()
}

onMounted(async () => {
  try {
    await store.loadMatrix()
  } catch (error) {
    store.error = error.message
  }
})
</script>

<template>
  <div class="terminal-shell">
    <aside class="sidebar">
      <div class="brand-kicker">C-A-B Research Terminal</div>
      <div class="brand-title">ETF 极端杠杆<br />压力研究台</div>
      <div class="brand-sub">
        点时化特征、CVaR reward shaping、事件式债务与可成交性约束的统一研究视图。
      </div>

      <div class="config-block">
        <label>策略消融组</label>
        <el-select v-model="config.group" class="full-select" size="large">
          <el-option
            v-for="group in groups"
            :key="group"
            :label="`${group} · ${group === 'G1' ? '动量规则' : group === 'G5' ? '随机基线' : group === 'G6' ? 'CVaR-PPO' : group === 'G4' ? '标准 PPO' : '分层 AI'}`"
            :value="group"
          />
        </el-select>

        <label>合成杠杆</label>
        <el-select v-model="config.leverage" class="full-select">
          <el-option
            v-for="value in leverages"
            :key="value"
            :label="`${value}x`"
            :value="value"
          />
        </el-select>

        <label>初始自有资金</label>
        <el-select v-model="config.initialCash" class="full-select">
          <el-option
            v-for="size in cashSizes"
            :key="size.value"
            :label="size.label"
            :value="size.value"
          />
        </el-select>

        <label>参数化费用情景</label>
        <el-select v-model="config.costScenario" class="full-select">
          <el-option label="低摩擦" value="low" />
          <el-option label="基准" value="base" />
          <el-option label="高摩擦" value="high" />
        </el-select>

        <label>展示 seed</label>
        <el-select v-model="config.seed" class="full-select">
          <el-option
            v-for="seed in config.group === 'G5' ? [11, 19, 31, 47, 59] : [17, 29, 43, 71, 101]"
            :key="seed"
            :label="`seed ${seed}`"
            :value="seed"
          />
        </el-select>

        <button class="run-button" :disabled="store.loading" @click="runReplay">
          {{ store.loading ? '正在回放 validation…' : '运行 validation 回放' }}
        </button>
      </div>

      <div class="warning-box">
        <strong>冻结口径 · 合成压力</strong><br />
        p0_1_frozen={{ frozenMeta.p0_1_frozen }} · synthetic_v1 ·
        parameterized_etf_v1。100x 不代表真实融资能力。Holdout 结果只读，不从此页面重跑。
      </div>
    </aside>

    <main class="content">
      <div class="topbar">
        <h1>组合决策、成交摩擦与生存状态</h1>
        <div class="status-pills">
          <span class="status-pill ready">DATA READY</span>
          <span class="status-pill holdout">HOLDOUT EVALUATED ONCE</span>
          <span class="status-pill">{{ frozenMeta.data_version?.slice(0, 20) }}…</span>
        </div>
      </div>

      <div v-if="store.error" class="error-banner">{{ store.error }}</div>

      <section class="metrics-grid">
        <div class="metric-card">
          <div class="metric-label">累计收益</div>
          <div class="metric-value" :class="metricTone(timelineMetrics.totalReturn)">
            {{ percent(timelineMetrics.totalReturn) }}
          </div>
          <div class="metric-foot">截至当前 · 费用后</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">最大回撤</div>
          <div class="metric-value negative">{{ percent(timelineMetrics.maxDrawdown) }}</div>
          <div class="metric-foot">当前时点及以前峰值回撤</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">年化 Sharpe</div>
          <div class="metric-value">{{ number(timelineMetrics.sharpe) }}</div>
          <div class="metric-foot">截至当前日收益估计</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">首次强平</div>
          <div
            class="metric-value"
            :class="timelineMetrics.firstLiquidationProbability ? 'negative' : 'positive'"
          >
            {{ percent(timelineMetrics.firstLiquidationProbability, 0) }}
          </div>
          <div class="metric-foot">{{ timelineMetrics.accountStatus || '未加载' }}</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">总佣金</div>
          <div class="metric-value">{{ money(timelineMetrics.totalCommission) }}</div>
          <div class="metric-foot">累计至当前时点</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">费率拖累</div>
          <div class="metric-value" :class="costTone(timelineMetrics.feeDrag)">
            {{ percent(timelineMetrics.feeDrag) }}
          </div>
          <div class="metric-foot">同期零费用反事实</div>
        </div>
      </section>

      <section class="main-grid">
        <div class="panel">
          <div class="panel-head">
            <div class="panel-title">
              {{ groupedCodes.find((item) => item.code === store.activeCode)?.name || '标的' }}
              <span style="color:#7591ad">({{ store.activeCode || '—' }})</span>
            </div>
            <div class="panel-note">RAW OHLC · MA5/10/20/30 · B/S/X</div>
          </div>
          <div class="chart-toolbar">
            <el-select
              :model-value="store.activeCode"
              filterable
              placeholder="切换 ETF"
              style="width: 260px"
              @change="chooseCode"
            >
              <el-option
                v-for="item in groupedCodes"
                :key="item.code"
                :label="`${item.name} (${item.code})`"
                :value="item.code"
              />
            </el-select>
            <span class="panel-note">
              {{ store.barsLoading ? '加载行情…' : `${store.bars.length} bars` }}
            </span>
            <div class="chart-zoom-group">
              <button type="button" title="缩小" @click="zoomKline('out')">−</button>
              <button type="button" title="复位缩放" @click="zoomKline('reset')">复位</button>
              <button type="button" title="放大" @click="zoomKline('in')">＋</button>
            </div>
          </div>
          <div v-if="store.bars.length" class="chart-wrap">
            <KLineChart
              ref="klineRef"
              :bars="store.bars"
              :trades="store.trades"
              :active-code="store.activeCode"
              :cutoff-date="currentSnapshot?.trading_date || ''"
            />
          </div>
          <div v-else class="empty-state">
            <div v-if="store.loading" class="loading-layer">
              <span class="pulse"></span> 首次加载正式 bundle 约需一分钟
            </div>
            <span v-else>选择参数并运行 validation 回放</span>
          </div>
        </div>

        <div class="panel">
          <div class="panel-head">
            <div class="panel-title">当日持仓与保证金</div>
            <div class="panel-note">{{ currentSnapshot?.trading_date || '—' }}</div>
          </div>
          <div v-if="currentSnapshot" class="position-list">
            <div class="position-row" style="color:#8195ad; font-weight:700">
              <span>标的</span><span class="num">股数</span><span class="num">市值</span>
            </div>
            <div
              v-for="position in currentSnapshot.positions"
              :key="position.code"
              class="position-row"
            >
              <div>
                <div class="position-name">{{ position.name }}</div>
                <div class="position-meta">{{ position.code }}</div>
              </div>
              <div class="num">
                {{ Number(position.size).toLocaleString() }}
                <div v-if="position.locked_size" class="locked">
                  锁 {{ position.locked_size }}
                </div>
              </div>
              <div class="num">{{ money(position.value) }}</div>
            </div>
            <div class="position-row">
              <span>权益</span><span></span><span class="num">{{ money(currentSnapshot.equity) }}</span>
            </div>
            <div class="position-row">
              <span>债务</span><span></span><span class="num">{{ money(currentSnapshot.debt) }}</span>
            </div>
            <div class="position-row">
              <span>保证金率</span><span></span>
              <span class="num">{{ percent(currentSnapshot.margin_ratio) }}</span>
            </div>
            <div class="position-row">
              <span>实际杠杆</span><span></span>
              <span class="num">{{ number(currentSnapshot.l_eff_actual) }}x</span>
            </div>
            <div class="position-row">
              <span>当日未成交订单</span><span></span>
              <span class="num">{{ currentSnapshot.unfilled_order_count || 0 }}</span>
            </div>
          </div>
          <div v-else class="empty-state" style="height:360px">等待快照</div>
        </div>
      </section>

      <section class="panel" style="margin-top:14px">
        <div class="panel-head">
          <div class="panel-title">虚拟时间轴</div>
          <div class="panel-note">前端本地 slice，不发起后端请求</div>
        </div>
        <Timeline
          v-model="timelineIndex"
          :snapshots="store.snapshots"
          :trades="store.trades"
        />
      </section>

      <section class="lower-grid">
        <div class="panel">
          <div class="panel-head">
            <div class="panel-title">逐笔成交与费用审计</div>
            <div class="panel-note">
              {{ store.trades.filter((trade) => trade.code === store.activeCode).length }} 笔
            </div>
          </div>
          <div class="table-wrap">
            <table class="mini-table">
              <thead>
                <tr>
                  <th>日期</th><th>标的</th><th>方向</th>
                  <th class="num">股数</th><th class="num">成交价</th>
                  <th class="num">佣金</th><th class="num">滑点</th>
                  <th class="num">未成交</th><th>原因</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="(trade, index) in store.trades.filter((item) => item.code === store.activeCode && (!currentSnapshot || item.execution_date <= currentSnapshot.trading_date)).slice(-80)"
                  :key="`${trade.execution_date}-${trade.side}-${index}`"
                >
                  <td>{{ trade.execution_date }}</td>
                  <td>{{ trade.name }}</td>
                  <td :class="trade.is_liquidation ? 'side-liquidate' : trade.side === 'buy' ? 'side-buy' : 'side-sell'">
                    {{ trade.is_liquidation ? '强平' : trade.side === 'buy' ? '买入' : '卖出' }}
                  </td>
                  <td class="num">{{ trade.shares }}</td>
                  <td class="num">{{ Number(trade.execution_price).toFixed(3) }}</td>
                  <td class="num">{{ Number(trade.commission).toFixed(2) }}</td>
                  <td class="num">{{ Number(trade.slippage_cost).toFixed(2) }}</td>
                  <td class="num">{{ trade.unfilled_shares }}</td>
                  <td>{{ trade.reason }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <div class="panel">
          <div class="panel-head">
            <div class="panel-title">账户状态</div>
            <div class="panel-note">{{ metrics.account_status || '—' }}</div>
          </div>
          <div v-if="currentSnapshot" class="position-list">
            <div class="position-row"><span>现金</span><span></span><span class="num">{{ money(currentSnapshot.cash) }}</span></div>
            <div class="position-row"><span>持仓市值</span><span></span><span class="num">{{ money(currentSnapshot.position_value) }}</span></div>
            <div class="position-row"><span>累计融资利息</span><span></span><span class="num">{{ money(currentSnapshot.cumulative_interest) }}</span></div>
            <div class="position-row"><span>累计其他费用</span><span></span><span class="num">{{ money(currentSnapshot.cumulative_other_fees) }}</span></div>
            <div class="position-row"><span>坏账</span><span></span><span class="num">{{ money(currentSnapshot.cumulative_bad_debt) }}</span></div>
            <div class="position-row"><span>融资模式</span><span></span><span class="num">{{ currentSnapshot.financing_mode }}</span></div>
          </div>
        </div>
      </section>

      <div class="matrix-head">
        <h2>Final Holdout 强平概率矩阵</h2>
        <span class="panel-note">base cost · ¥1,000,000 · 点击单元格选择杠杆</span>
      </div>
      <div class="panel">
        <table class="matrix-table">
          <thead>
            <tr>
              <th>杠杆</th>
              <th v-for="group in groups" :key="group">{{ group }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(cell, index) in baseMatrix" :key="leverages[index]">
              <th>{{ leverages[index] }}x</th>
              <td
                v-for="group in groups"
                :key="group"
                class="matrix-cell"
                :style="liquidCellStyle(cell, group)"
                @click="selectMatrix(cell)"
              >
                {{ percent(pickGroup(cell, group).first_liquidation_probability, 0) }}
                <div style="font-size:9px;opacity:.75">
                  R {{ percent(pickGroup(cell, group).total_return?.mean) }}
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </main>
  </div>
</template>

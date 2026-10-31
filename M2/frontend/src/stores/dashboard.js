import { defineStore } from 'pinia'

const API = import.meta.env.VITE_API_BASE || '/api'

export const useDashboardStore = defineStore('dashboard', {
  state: () => ({
    matrix: null,
    result: null,
    bars: [],
    activeCode: '',
    loading: false,
    barsLoading: false,
    error: '',
    cache: {},
    barRequestId: 0,
  }),
  getters: {
    metadata: (state) => state.matrix?.metadata || {},
    primary: (state) => state.matrix?.primary || {},
    metrics: (state) => state.result?.metrics || {},
    snapshots: (state) => state.result?.daily_snapshot || [],
    trades: (state) => state.result?.trades || [],
  },
  actions: {
    async loadMatrix() {
      const response = await fetch(`${API}/matrix`)
      if (!response.ok) throw new Error(`Matrix request failed: ${response.status}`)
      this.matrix = await response.json()
    },
    async runBacktest(config) {
      this.loading = true
      this.error = ''
      const key = JSON.stringify(config)
      try {
        if (this.cache[key]) {
          this.result = this.cache[key]
        } else {
          const params = new URLSearchParams({
            group: config.group,
            leverage: config.leverage,
            initial_cash: config.initialCash,
            cost_scenario: config.costScenario,
            seed: config.seed,
          })
          const response = await fetch(`${API}/backtest?${params}`)
          if (!response.ok) {
            const body = await response.json().catch(() => ({}))
            throw new Error(body.detail || `Backtest request failed: ${response.status}`)
          }
          this.result = await response.json()
          this.cache[key] = this.result
        }
        const available = new Set(
          this.result.trades.map((trade) => trade.code),
        )
        const positions = new Set(
          this.result.daily_snapshot.flatMap((snapshot) =>
            (snapshot.positions || []).map((position) => position.code),
          ),
        )
        const candidates = new Set(
          this.result.target_portfolio.flatMap((target) =>
            Object.keys(target.weights || {}),
          ),
        )
        this.activeCode =
          [...available][0] || [...positions][0] || [...candidates][0] || ''
        if (this.activeCode) await this.loadBars(this.activeCode)
      } catch (error) {
        this.error = error.message
      } finally {
        this.loading = false
      }
    },
    async loadBars(code) {
      if (!code) return
      const requestId = ++this.barRequestId
      this.activeCode = code
      this.bars = []
      this.barsLoading = true
      try {
        const response = await fetch(`${API}/etf/${code}/daily`)
        if (!response.ok) throw new Error(`K-line request failed: ${response.status}`)
        const payload = await response.json()
        if (requestId === this.barRequestId && code === this.activeCode) {
          this.bars = payload.bars
        }
      } catch (error) {
        if (requestId === this.barRequestId) {
          this.error = error.message
          this.bars = []
        }
      } finally {
        if (requestId === this.barRequestId) this.barsLoading = false
      }
    },
  },
})

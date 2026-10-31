<script setup>
import { computed } from 'vue'

const props = defineProps({
  modelValue: { type: Number, default: 0 },
  snapshots: { type: Array, default: () => [] },
  trades: { type: Array, default: () => [] },
})
const emit = defineEmits(['update:modelValue'])

const maxIndex = computed(() => Math.max(0, props.snapshots.length - 1))
const currentDate = computed(
  () => props.snapshots[props.modelValue]?.trading_date || '尚未加载',
)
const visibleTradeCount = computed(
  () =>
    props.trades.filter((trade) => trade.execution_date <= currentDate.value)
      .length,
)

function update(event) {
  emit('update:modelValue', Number(event.target.value))
}
</script>

<template>
  <div class="timeline-shell">
    <input
      class="timeline-range"
      type="range"
      min="0"
      :max="maxIndex"
      :value="modelValue"
      @input="update"
    />
    <div class="timeline-meta">
      <span>{{ currentDate }}</span>
      <span>{{ snapshots.length }} 个交易日 · {{ visibleTradeCount }} 笔成交</span>
    </div>
  </div>
</template>

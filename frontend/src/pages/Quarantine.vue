<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'

const rows = ref([])
const err = ref('')
const expanded = ref({})

function toggle(id) {
  expanded.value[id] = !expanded.value[id]
}

function pretty(raw) {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2)
  } catch {
    return raw
  }
}

onMounted(async () => {
  try {
    rows.value = await api.quarantine()
  } catch (e) {
    err.value = String(e.message || e)
  }
})
</script>

<template>
  <section class="panel">
    <h1>搁置区</h1>
    <p class="muted">
      缺必填字段的脏点在收流入口即被剔入此表，保留原始 payload 与原因；不进正式成员、
      不更新 open 末击点。点击行可展开 payload。
    </p>
    <p v-if="err" class="err">{{ err }}</p>
    <p v-if="!rows.length" class="muted">暂无搁置记录。</p>
    <table v-else class="data">
      <thead>
        <tr><th>搁置键</th><th>批次</th><th>访客</th><th>原因</th><th>时刻</th><th>payload</th></tr>
      </thead>
      <tbody>
        <template v-for="r in rows" :key="r.id">
          <tr class="hold-row" @click="toggle(r.id)">
            <td class="mono">{{ r.hold_key || r.id }}</td>
            <td class="mono">{{ r.batch_id || '—' }}</td>
            <td>{{ r.visitor_id || '（无 visitor_id）' }}</td>
            <td><span class="reason-tag">{{ r.reason }}</span></td>
            <td class="mono">{{ r.created_at }}</td>
            <td><span class="linkish">{{ expanded[r.id] ? '收起 ▲' : '展开 ▼' }}</span></td>
          </tr>
          <tr v-if="expanded[r.id]" class="payload-row">
            <td colspan="6">
              <pre class="mono payload">{{ pretty(r.member_json) }}</pre>
            </td>
          </tr>
        </template>
      </tbody>
    </table>
  </section>
</template>

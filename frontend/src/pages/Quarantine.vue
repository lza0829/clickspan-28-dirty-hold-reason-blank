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
      缺必填字段的脏点在收流入口即被剔入此表，入 hold 同事务固化非空原因、原因码与当时
      规则版本快照；不进正式成员、不更新 open 末击点。原因与版本<strong>不可变</strong>，
      规则改版只影响之后收流，不回溯本行。点击行可展开 payload 与规则快照。
    </p>
    <p v-if="err" class="err">{{ err }}</p>
    <p v-if="!rows.length" class="muted">暂无搁置记录。</p>
    <table v-else class="data">
      <thead>
        <tr>
          <th>搁置键</th><th>批次</th><th>访客</th><th>原因</th><th>原因码</th>
          <th>规则版本</th><th>时刻</th><th>payload</th>
        </tr>
      </thead>
      <tbody>
        <template v-for="r in rows" :key="r.id">
          <tr class="hold-row" @click="toggle(r.id)">
            <td class="mono">{{ r.hold_key || r.id }}</td>
            <td class="mono">{{ r.batch_id || '—' }}</td>
            <td>{{ r.visitor_id || '（无 visitor_id）' }}</td>
            <td><span class="reason-tag">{{ r.reason }}</span></td>
            <td class="mono">{{ r.reason_code }}</td>
            <td class="mono">{{ r.rule_version }}</td>
            <td class="mono">{{ r.created_at }}</td>
            <td><span class="linkish">{{ expanded[r.id] ? '收起 ▲' : '展开 ▼' }}</span></td>
          </tr>
          <tr v-if="expanded[r.id]" class="payload-row">
            <td colspan="8">
              <p class="muted">原始 payload：</p>
              <pre class="mono payload">{{ pretty(r.member_json) }}</pre>
              <p class="muted">固化规则快照（不可变）：</p>
              <pre class="mono payload">{{ pretty(r.rule_snapshot_json) }}</pre>
            </td>
          </tr>
        </template>
      </tbody>
    </table>
  </section>
</template>

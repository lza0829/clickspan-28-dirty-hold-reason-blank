<script setup>
import { ref } from 'vue'
import { api } from '../api.js'

const result = ref(null)
const err = ref('')
const busy = ref(false)

async function run() {
  busy.value = true
  err.value = ''
  result.value = null
  try {
    result.value = await api.ingest()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <!-- plant-ui -->

  <section class="panel">
    <h1>收流击点</h1>
    <p class="muted">
      读取夹具 ingest_batch.json。入口先做必填字段校验（visitor_id / event_ts / page_path /
      event_name）：脏点剔入搁置并保留原始 payload，合法点才写正式、推进 open 末击点。
    </p>
    <button class="btn" :disabled="busy" @click="run">{{ busy ? '收流中…' : '跑一批收流' }}</button>
    <p v-if="err" class="err">{{ err }}</p>

    <div v-if="result?.ok" class="ingest-report">
      <h2>批次报告 <span class="mono muted">{{ result.report.batch_id }}</span></h2>
      <div class="stats">
        <div class="stat"><b>{{ result.report.input }}</b><span>输入击点</span></div>
        <div class="stat"><b class="ok">{{ result.report.written }}</b><span>正式写入</span></div>
        <div class="stat"><b class="warn">{{ result.report.held }}</b><span>剔入搁置</span></div>
      </div>
      <p v-if="result.report.held" class="muted">
        搁置键（{{ result.report.hold_keys.length }}）：
      </p>
      <ul v-if="result.report.held" class="hold-keys mono">
        <li v-for="k in result.report.hold_keys" :key="k">{{ k }}</li>
      </ul>
      <p v-else class="ok">本批无脏点。</p>
    </div>
    <pre v-else-if="result" class="mono err">{{ result }}</pre>
  </section>
</template>

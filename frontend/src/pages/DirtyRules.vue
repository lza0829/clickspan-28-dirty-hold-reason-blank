<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'

const ALL_FIELDS = [
  { field: 'visitor_id', label: 'visitor_id' },
  { field: 'event_ts', label: 'event_ts' },
  { field: 'page_path', label: 'page_path' },
  { field: 'event_name', label: 'event_name' },
]

const data = ref(null)
const picked = ref([])
const note = ref('')
const err = ref('')
const msg = ref('')
const busy = ref(false)

function prettySnapshot(rule) {
  return JSON.stringify(rule.snapshot || rule, null, 2)
}

async function load() {
  err.value = ''
  try {
    data.value = await api.dirtyRules()
    const cur = data.value.versions.find((v) => v.version === data.value.current_version)
    picked.value = cur ? [...cur.required_fields] : ALL_FIELDS.map((f) => f.field)
  } catch (e) {
    err.value = String(e.message || e)
  }
}

async function publish() {
  err.value = ''
  msg.value = ''
  if (!picked.value.length) {
    err.value = '至少保留一个必填字段，规则清单不能为空'
    return
  }
  busy.value = true
  try {
    const res = await api.publishDirtyRules(picked.value, note.value.trim())
    msg.value = `已发布新版本 ${res.current_version}；仅对之后收流生效，已落 hold 的原因与版本快照不可变。`
    note.value = ''
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

onMounted(load)
</script>

<template>
  <section class="panel">
    <h1>缺字段规则版本</h1>
    <p class="muted">
      每次保存都追加一个不可变的新版本（版本 ↔ 原因码对照），并切换当前生效版本。
      新版本只影响<strong>之后</strong>的收流；已落 hold 的原因、原因码与规则版本快照
      由数据库触发器保护，任何改写都会被回滚。
    </p>
    <p v-if="err" class="err">{{ err }}</p>
    <p v-if="msg" class="ok">{{ msg }}</p>

    <div v-if="data" class="rules-current">
      <h2>当前生效版本 <span class="mono reason-tag">{{ data.current_version }}</span></h2>
      <div class="rule-editor">
        <p class="muted">勾选新版本的必填字段（缺字段清单）：</p>
        <label v-for="f in ALL_FIELDS" :key="f.field" class="rule-check">
          <input type="checkbox" :value="f.field" v-model="picked" />
          <span class="mono">{{ f.label }}</span>
        </label>
        <div>
          <input
            v-model="note"
            class="mono rule-note"
            placeholder="版本说明（可选），如：收窄必填清单"
          />
        </div>
        <button class="btn" :disabled="busy" @click="publish">
          {{ busy ? '发布中…' : '发布新版本（不回溯旧 hold）' }}
        </button>
      </div>
    </div>

    <h2>历史版本对照（只追加，不可改）</h2>
    <table v-if="data?.versions?.length" class="data">
      <thead>
        <tr><th>版本</th><th>序号</th><th>必填字段 / 原因码对照</th><th>发布时刻</th><th>说明</th></tr>
      </thead>
      <tbody>
        <tr v-for="v in data.versions" :key="v.version">
          <td class="mono">
            {{ v.version }}
            <span v-if="v.version === data.current_version" class="reason-tag">当前</span>
          </td>
          <td class="mono">{{ v.seq }}</td>
          <td>
            <div v-for="(meta, field) in v.reason_codes" :key="field" class="reason-line">
              <span class="mono">{{ field }}</span>
              → <span class="mono">{{ meta.code }}</span>
              <em>（{{ meta.label }}）</em>
            </div>
          </td>
          <td class="mono">{{ v.created_at }}</td>
          <td>{{ v.note || '—' }}</td>
        </tr>
      </tbody>
    </table>
    <p v-else class="muted">暂无规则版本。</p>
  </section>
</template>

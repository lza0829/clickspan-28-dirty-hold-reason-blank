<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'

const rows = ref([])
const err = ref('')
const att = ref(null)
const attErr = ref('')
const reasonAtt = ref(null)
const batchId = ref('')

async function loadProofs() {
  err.value = ''
  try {
    rows.value = await api.proofs()
  } catch (e) {
    err.value = String(e.message || e)
  }
}

async function loadAttestation() {
  att.value = null
  attErr.value = ''
  reasonAtt.value = null
  const bid = batchId.value.trim() || undefined
  try {
    att.value = await api.dirtyAttestation(bid)
  } catch (e) {
    attErr.value = String(e.message || e)
  }
  try {
    reasonAtt.value = await api.reasonAttestation(bid)
  } catch (e) {
    attErr.value = [attErr.value, String(e.message || e)].filter(Boolean).join('；')
  }
}

onMounted(async () => {
  await loadProofs()
  await loadAttestation()
})
</script>

<template>
  <!-- plant-ui -->

  <section class="panel">
    <h1>会话核对</h1>
    <p class="muted">会话击点集合与起止须与织机汇总可核。</p>
    <p v-if="err" class="err">{{ err }}</p>
    <table class="data">
      <thead>
        <tr><th>会话</th><th>访客</th><th>点数</th><th>起止</th><th>击点集</th></tr>
      </thead>
      <tbody>
        <tr v-for="r in rows" :key="r.id">
          <td class="mono">{{ r.session_id }}</td>
          <td>{{ r.visitor_id }}</td>
          <td>{{ r.hit_count }}</td>
          <td class="mono">{{ r.started_at }} → {{ r.ended_at }}</td>
          <td class="mono">{{ r.hit_ids_json }}</td>
        </tr>
      </tbody>
    </table>
  </section>

  <section class="panel">
    <h1>脏点核对（只读）</h1>
    <p class="muted">
      核对收流批次：搁置键与原因、正式写入数，以及 open 会话末击点是否被脏行顶替
      （当前 ended_at 必须等于正式成员最大 event_ts）。
    </p>
    <div class="att-bar">
      <input
        v-model="batchId"
        class="mono"
        placeholder="留空核对最新批次，或填 batch_id"
        @keyup.enter="loadAttestation"
      />
      <button class="btn" @click="loadAttestation">核对</button>
    </div>
    <p v-if="attErr" class="err">{{ attErr }}</p>
    <div v-if="att" class="att-body">
      <div class="stats">
        <div class="stat"><b>{{ att.batch_id }}</b><span>批次</span></div>
        <div class="stat"><b>{{ att.input }}</b><span>输入</span></div>
        <div class="stat"><b class="ok">{{ att.written }}</b><span>正式写入</span></div>
        <div class="stat"><b class="warn">{{ att.held }}</b><span>搁置</span></div>
        <div class="stat">
          <b :class="att.passed ? 'ok' : 'err'">{{ att.passed ? '通过' : '不通过' }}</b>
          <span>核对结论</span>
        </div>
      </div>

      <table class="data">
        <thead>
          <tr><th>搁置键</th><th>访客</th><th>原因</th><th>原因码</th><th>规则版本</th><th>时刻</th></tr>
        </thead>
        <tbody>
          <tr v-for="r in att.held_rows" :key="r.id">
            <td class="mono">{{ r.hold_key || r.id }}</td>
            <td>{{ r.visitor_id || '（无）' }}</td>
            <td><span class="reason-tag">{{ r.reason }}</span></td>
            <td class="mono">{{ r.reason_code }}</td>
            <td class="mono">{{ r.rule_version }}</td>
            <td class="mono">{{ r.created_at }}</td>
          </tr>
          <tr v-if="!att.held_rows.length">
            <td colspan="6" class="muted">本批无搁置点</td>
          </tr>
        </tbody>
      </table>

      <h2>hold 原因核证（只读，与搁置页同源）</h2>
      <p class="muted">
        逐行核：脏键、固化原因与原因码、入 hold 时的规则版本快照与登记表是否一致。
        数据全部读 hold 行固化值，规则事后改版不会改变本结论。
      </p>
      <p v-if="reasonAtt">
        总体结论：
        <strong :class="reasonAtt.passed ? 'ok' : 'err'">
          {{ reasonAtt.passed ? '全部通过：原因非空且版本快照可核' : '不通过：存在原因/版本分叉' }}
        </strong>
      </p>
      <table v-if="reasonAtt?.items?.length" class="data">
        <thead>
          <tr>
            <th>脏键</th><th>reason</th><th>reason_code</th><th>规则版本</th>
            <th>快照对照</th><th>结论</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(it, i) in reasonAtt.items" :key="i">
            <td class="mono">{{ it.dirty_key }}</td>
            <td><span class="reason-tag">{{ it.reason }}</span></td>
            <td class="mono">{{ it.reason_code }}</td>
            <td class="mono">{{ it.rule_version }}</td>
            <td>
              <span :class="it.snapshot_match ? 'ok' : 'err'">
                {{ it.snapshot_match ? '与登记表一致' : '快照不一致' }}
              </span>
            </td>
            <td :class="it.passed ? 'ok' : 'err'">{{ it.conclusion }}</td>
          </tr>
        </tbody>
      </table>

      <h2>末击点时钟比对</h2>
      <p>
        状态：
        <strong :class="att.clock_unchanged ? 'ok' : 'err'">
          {{ att.clock_unchanged ? '末击点未漂（仅由正式成员推出）' : '末击点漂移（疑似脏行顶替）' }}
        </strong>
      </p>
      <table class="data">
        <thead>
          <tr><th>会话</th><th>批次前 ended_at</th><th>当前 ended_at</th><th>正式成员应得末击点</th></tr>
        </thead>
        <tbody>
          <tr v-for="(after, sid) in att.clock_after" :key="sid">
            <td class="mono">{{ sid }}</td>
            <td class="mono">{{ att.clock_before[sid] }}</td>
            <td class="mono" :class="after === att.clock_expected[sid] ? 'ok' : 'err'">
              {{ after }}
            </td>
            <td class="mono">{{ att.clock_expected[sid] }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

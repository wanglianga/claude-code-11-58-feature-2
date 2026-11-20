<template>
  <div v-if="run">
    <el-card>
      <template #header>
        <div class="bar">
          <span>
            回馈去向推导 ·
            <el-tag size="small" type="info">{{ run.run_key }}</el-tag>
            <el-tag v-if="run.version > 1" size="small" type="warning" style="margin-left:4px">
              v{{ run.version }}
            </el-tag>
          </span>
          <div>
            <el-radio-group v-model="route" size="small" @change="load">
              <el-radio-button value="CONSERVATION">路线A 守恒分配</el-radio-button>
              <el-radio-button value="LEARNED">路线B 学习预测</el-radio-button>
            </el-radio-group>
            <el-button size="small" style="margin-left: 8px" @click="rederive">重新推导</el-button>
          </div>
        </div>
      </template>
      <el-alert v-if="advice" :closable="false" type="info" show-icon>
        <template #title>
          路线建议:<b>{{ ROUTE_LABEL[advice.recommended_route] }}</b>
          (分区计量完整度 {{ (advice.metering_completeness * 100).toFixed(0) }}%,
          运行模式稳定性 {{ (advice.operation_stability * 100).toFixed(0) }}%)
        </template>
        <div class="hint">{{ advice.rationale }}</div>
      </el-alert>
      <el-empty v-if="!d" description="该路线暂无推导结论,可点击重新推导" />
      <template v-else>
        <el-descriptions :column="4" border size="small" style="margin-top: 12px">
          <el-descriptions-item label="推导状态">
            <el-tag :type="STATUS_TAG[d.status]">{{ d.status }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="去向版本">v{{ d.version }}</el-descriptions-item>
          <el-descriptions-item label="产生回馈">{{ fmtBand(d.regen_available) }} kWh</el-descriptions-item>
          <el-descriptions-item label="范围">
            {{ d.range_start.slice(11, 16) }} ~ {{ d.range_end.slice(11, 16) }}
          </el-descriptions-item>
          <el-descriptions-item label="已确认节电(可宣称)" :span="2">
            <b class="ok">{{ fmtBand(d.basis.summary.eligible_confirmed) }} kWh</b>
          </el-descriptions-item>
          <el-descriptions-item label="潜在上限(含可能吸收)" :span="2">
            {{ fmtBand(d.basis.summary.eligible_max) }} kWh
          </el-descriptions-item>
        </el-descriptions>

        <el-row :gutter="16" style="margin-top: 14px">
          <el-col :span="13">
            <h4>去向分类与节能资格</h4>
            <el-table :data="destRows" size="small">
              <el-table-column label="去向" width="150">
                <template #default="{ row }">{{ row.label }}</template>
              </el-table-column>
              <el-table-column label="kWh" width="170">
                <template #default="{ row }">{{ fmtBand(row.band) }}</template>
              </el-table-column>
              <el-table-column label="节能资格" width="110">
                <template #default="{ row }">
                  <el-tag size="small" :type="QUAL_TAG[row.qualified]">{{ row.qualified }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="说明">
                <template #default="{ row }"><span class="st">{{ row.statement }}</span></template>
              </el-table-column>
            </el-table>
          </el-col>
          <el-col :span="11">
            <h4>分区守恒分配(不得跨分区抵销)</h4>
            <el-table :data="d.allocations" size="small">
              <el-table-column prop="zone" label="分区" width="70" />
              <el-table-column label="产生回馈" width="110">
                <template #default="{ row }">{{ fmtBand(row.regen_available) }}</template>
              </el-table-column>
              <el-table-column label="边界" width="80">
                <template #default="{ row }">
                  <el-tag size="small" :type="row.boundary && row.boundary.complete ? 'success' : 'danger'">
                    {{ row.boundary && row.boundary.complete ? '闭合' : '未闭合' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="邻车" width="80">
                <template #default="{ row }">{{ row.neighbor ? row.neighbor.count : '-' }}</template>
              </el-table-column>
              <el-table-column label="去向摘要">
                <template #default="{ row }">{{ allocSummary(row) }}</template>
              </el-table-column>
            </el-table>
          </el-col>
        </el-row>

        <template v-if="route === 'LEARNED'">
          <h4 style="margin-top: 16px">学习模型依据</h4>
          <el-descriptions :column="3" border size="small">
            <el-descriptions-item label="模型">{{ d.basis.model || '样本不足,未建模' }}</el-descriptions-item>
            <el-descriptions-item label="训练样本">{{ d.basis.train_samples ?? '-' }}</el-descriptions-item>
            <el-descriptions-item label="R² / 残差σ">
              {{ d.basis.r2 ?? '-' }} / {{ d.basis.residual_std ?? '-' }}
            </el-descriptions-item>
          </el-descriptions>
          <el-table :data="d.allocations" size="small" style="margin-top: 10px">
            <el-table-column prop="zone" label="分区" width="70" />
            <el-table-column label="预测吸收比例">
              <template #default="{ row }">
                <span v-if="row.predicted_fraction === null || row.predicted_fraction === undefined">-</span>
                <span v-else>
                  {{ (row.predicted_fraction * 100).toFixed(1) }}%
                  (区间 {{ (row.prediction_interval[0] * 100).toFixed(1) }}% ~
                  {{ (row.prediction_interval[1] * 100).toFixed(1) }}%)
                </span>
              </template>
            </el-table-column>
            <el-table-column label="特征(负荷/车距/坡度/装置可用)">
              <template #default="{ row }">
                <span v-if="row.features">
                  {{ row.features.load_per_min }} / {{ row.features.headway_min }}min /
                  {{ row.features.gradient_permille }}‰ / {{ row.features.device_available }}
                </span>
                <span v-else>-</span>
              </template>
            </el-table-column>
          </el-table>
        </template>

        <el-row :gutter="16" style="margin-top: 16px">
          <el-col :span="14">
            <h4>推导依据</h4>
            <ul class="reasons">
              <li v-for="(r, i) in d.basis.reasons" :key="i">{{ r }}</li>
            </ul>
          </el-col>
          <el-col :span="10">
            <h4>约束提示 / 警告</h4>
            <el-empty v-if="!d.warnings.length" description="无" :image-size="60" />
            <el-table v-else :data="d.warnings" size="small">
              <el-table-column prop="code" label="代码" width="180" />
              <el-table-column prop="detail" label="说明" />
            </el-table>
          </el-col>
        </el-row>

        <h4 style="margin-top: 16px">去向版本历史(状态晚到只增版本,不追溯改写)</h4>
        <el-table :data="historyForRoute" size="small">
          <el-table-column label="版本" width="80">
            <template #default="{ row }">v{{ row.version }}</template>
          </el-table-column>
          <el-table-column prop="status" label="状态" width="110" />
          <el-table-column prop="created_at" label="生成时间" width="180" />
          <el-table-column label="取代" width="90">
            <template #default="{ row }">{{ row.supersedes_id ?? '-' }}</template>
          </el-table-column>
        </el-table>
      </template>
    </el-card>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import api from '../api'
import { fmtBand } from '../utils'

const routeParam = useRoute()
const run = ref(null)
const data = ref(null)
const advice = ref(null)
const route = ref('CONSERVATION')

const ROUTE_LABEL = { CONSERVATION: '路线A 守恒分配', LEARNED: '路线B 学习预测' }
const STATUS_TAG = { READY: 'success', PARTIAL: 'warning', INSUFFICIENT: 'danger' }
const QUAL_TAG = { YES: 'success', CONDITIONAL: 'warning', NO: 'danger',
                   UNDETERMINED: 'info', GRID_ONLY: 'info' }
const DEST_ORDER = ['neighbor_confirmed', 'device_confirmed', 'possible_absorption',
                    'limited_dissipation', 'unknown_source', 'exported_to_grid']

const d = computed(() => (data.value ? data.value.routes[route.value] : null))
const historyForRoute = computed(() =>
  data.value ? data.value.history.filter((h) => h.route === route.value) : [])
const destRows = computed(() => {
  if (!d.value) return []
  return DEST_ORDER.map((k) => ({
    label: d.value.dest_label[k],
    band: d.value.destinations[k],
    ...d.value.eligibility[k],
  }))
})

function allocSummary(row) {
  if (!row.destinations) {
    return row.predicted_fraction !== undefined ? `预测吸收 ${(row.predicted_fraction * 100).toFixed(0)}%` : '-'
  }
  return Object.entries(row.destinations)
    .filter(([, b]) => b.v)
    .map(([k, b]) => `${d.value.dest_label[k]} ${b.v}`)
    .join(';') || '-'
}

async function load() {
  data.value = (await api.get(`/runs/${routeParam.params.id}/regen`)).data
}

async function rederive() {
  await api.post(`/runs/${routeParam.params.id}/regen/derive`, null,
                 { params: { route: route.value } })
  await load()
}

onMounted(async () => {
  run.value = (await api.get(`/runs/${routeParam.params.id}`)).data
  advice.value = (await api.get(`/runs/${routeParam.params.id}/regen/advice`)).data
  await load()
})
</script>

<style scoped>
.bar { display: flex; justify-content: space-between; align-items: center; }
.hint { color: #666; font-size: 12px; margin-top: 4px; }
.ok { color: #1a7f37; }
.st { color: #666; font-size: 12px; }
.reasons { margin: 0; padding-left: 18px; font-size: 13px; color: #555; }
.reasons li { margin-bottom: 4px; }
h4 { margin: 0 0 8px; color: #555; }
</style>

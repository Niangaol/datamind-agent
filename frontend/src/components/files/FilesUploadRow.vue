<script setup lang="ts">
import FilesUploadDetail from './FilesUploadDetail.vue'
import { APPROVE_GATE_NOTE, POLL_EXHAUSTED_TEXT, type FilesRowVM } from './uploadHonesty'

/**
 * 一行上传记录（列表行与本批上传行共用）。
 *
 * WHY 共用：同一份数据在「上传记录」和「本批上传」两处出现，如果用两套模板，
 * 就会出现「列表说待复核、本批说已入库」这种自相矛盾。诚实措辞只允许有一个来源
 * （uploadHonesty.ts 的 buildFilesRow），这里只负责排版。
 *
 * 这一行**必须**同时出现的东西：
 *   status / searchable / index_state / index_note 四个字段，
 *   以及 queued+worker 未运行时的「后台处理器未运行」提示——缺一个都是失败。
 */
const props = defineProps<{ row: FilesRowVM }>()
const emit = defineEmits<{
  (e: 'toggle', row: FilesRowVM): void
  (e: 'approve', row: FilesRowVM): void
}>()

function onToggle(): void {
  emit('toggle', props.row)
}

function onApprove(): void {
  emit('approve', props.row)
}
</script>

<template>
  <li class="fur-row" :class="{ 'is-open': row.open }" data-testid="files-row" :data-upload-id="row.uploadId || ''">
    <div class="fur-head">
      <button
        class="fur-main"
        type="button"
        data-testid="files-row-toggle"
        :title="row.open ? '收起详情' : '展开详情'"
        @click="onToggle"
      >
        <span class="fur-name" :title="row.filename">{{ row.filename }}</span>
        <span class="fur-type">{{ row.mediaType || '类型未给出' }}</span>
        <span class="fur-when">{{ row.createdAtText || '时间未给出' }}</span>
        <span class="fur-badge" :class="`tone-${row.tone}`" data-testid="files-row-status">
          {{ row.statusText }}
        </span>
        <span v-if="row.approved" class="fur-badge tone-ok" data-testid="files-approved-badge">已复核</span>
        <span class="fur-caret" aria-hidden="true">{{ row.open ? '▾' : '▸' }}</span>
      </button>

      <button
        v-if="row.approvable"
        class="fur-approve"
        type="button"
        data-testid="files-approve"
        :disabled="row.approveBusy"
        @click="onApprove"
      >
        {{ row.approveBusy ? '复核中…' : '复核通过' }}
      </button>
    </div>

    <!-- 复核闸门说明：只要有记录就显示，不允许在提供「复核通过」的界面上省略它 -->
    <p v-if="row.uploadId" class="fur-gate" data-testid="files-approve-gate">
      {{ APPROVE_GATE_NOTE }}
    </p>
    <p
      v-if="row.uploadId && !row.approvable && !row.approved && row.status"
      class="fur-gate"
      data-testid="files-not-approvable"
    >
      当前状态不能复核：只有「已入库」与「部分失败」可以通过复核闸门。
    </p>

    <!-- 四个诚实字段：能否被检索 / 索引状态 / 索引说明 + 状态本身 -->
    <div class="fur-fields">
      <span class="fur-f" data-testid="files-row-searchable">{{ row.searchableText }}</span>
      <span class="fur-f" data-testid="files-row-index-state">{{ row.indexStateText }}</span>
      <span class="fur-f" data-testid="files-row-index-note">{{ row.indexNoteText }}</span>
      <span v-if="row.blockCount != null" class="fur-f">
        契约块 {{ row.blockCount }} · 定位记录 {{ row.locatorCount ?? 0 }}
      </span>
    </div>

    <!-- 建索引失败：解析成功但索引没写成，必须单独说清（不能伪装成解析失败，
         也不能换一句「暂不可问答」把原因码丢掉） -->
    <p v-if="row.indexErrorText" class="fur-warn" data-testid="files-row-index-error">
      {{ row.indexErrorText }}
    </p>

    <!-- 本地阶段（预检被拒 / 上传中 / 轮询中）——本来就不是服务端状态，单独一行说明 -->
    <p v-if="row.phaseText" class="fur-note" data-testid="files-row-phase">{{ row.phaseText }}</p>

    <!-- worker 诚实：queued 且处理器未运行时，这一句取代一切「正在解析」的说法 -->
    <p v-if="row.workerWarning" class="fur-warn" data-testid="files-row-worker-warning">
      {{ row.workerWarning }}
    </p>

    <p v-if="row.pollExhausted" class="fur-warn" data-testid="files-row-poll-exhausted">
      {{ POLL_EXHAUSTED_TEXT }}
    </p>

    <p v-if="row.errorText" class="fur-error" data-testid="files-row-error">{{ row.errorText }}</p>

    <template v-if="row.approveNotice">
      <p
        class="fur-notice"
        :class="`tone-${row.approveNotice.tone}`"
        data-testid="files-approval-notice"
      >
        {{ row.approveNotice.text }}
      </p>
    </template>

    <FilesUploadDetail
      v-if="row.open"
      :detail="row.detail"
      :loading="row.detailLoading"
      :error="row.detailError"
    />
  </li>
</template>

<style scoped>
/* 类名统一 fur- 前缀，避免与 Tailwind v4 工具类重名（仓库已出过两次事故）。 */
.fur-row {
  list-style: none;
  border: 1px solid var(--color-line);
  border-radius: 12px;
  background: var(--color-panel);
  padding: 8px 12px 10px;
}
.fur-row.is-open {
  border-color: var(--color-accent);
  background: rgba(37, 99, 235, 0.04);
}
.fur-head {
  display: flex;
  align-items: center;
  gap: 10px;
}
.fur-main {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 10px;
  font: inherit;
  text-align: left;
  padding: 2px 0;
  border: none;
  background: transparent;
  color: var(--color-body);
  cursor: pointer;
}
.fur-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 14.5px;
  color: var(--color-text);
}
.fur-type {
  flex-shrink: 0;
  font-size: 12.5px;
  color: var(--color-text);
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.fur-when {
  flex-shrink: 0;
  font-size: 13px;
  color: var(--color-muted);
  font-variant-numeric: tabular-nums;
}
/* 徽标统一蓝底白字加粗椭圆；tone-warn / tone-bad 是判据（橙/红），保留 */
.fur-badge {
  flex-shrink: 0;
  font-size: 12.5px;
  font-weight: 600;
  padding: 2px 9px;
  border-radius: 999px;
  border: 1px solid transparent;
  background: var(--color-accent);
  color: #ffffff;
}
.fur-badge.tone-ok {
  color: #ffffff; font-weight: 600;
  border-color: transparent;
  background: var(--color-accent);
}
.fur-badge.tone-warn {
  color: #ffffff; font-weight: 600;
  border-color: transparent;
  background: #d97706;
}
.fur-badge.tone-bad {
  color: #ffffff; font-weight: 600;
  border-color: transparent;
  background: #e11d48;
}
/* tone-muted 现在与基础样式等价（保留类名，避免动模板） */
.fur-badge.tone-muted {
  color: #ffffff;
  font-weight: 600;
  border-color: transparent;
  background: var(--color-accent);
}
.fur-caret {
  flex-shrink: 0;
  font-size: 12.5px;
  color: var(--color-muted);
}
/* 「审核」统一成实心蓝（原来是浅蓝底蓝字，与全站按钮不一致） */
.fur-approve {
  flex-shrink: 0;
  font: inherit;
  font-size: 13.5px;
  font-weight: 600;
  padding: 5px 14px;
  border-radius: 999px;
  border: 1px solid transparent;
  background: var(--color-accent);
  color: #ffffff;
  cursor: pointer;
}
.fur-approve:hover:not(:disabled) {
  background: #1d4ed8;
  color: #ffffff;
}
.fur-approve:disabled {
  opacity: 0.6;
  cursor: default;
}
.fur-gate {
  margin: 4px 0 0;
  font-size: 12.5px;
  color: var(--color-text);
}
.fur-fields {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 14px;
  margin-top: 5px;
}
.fur-f {
  font-size: 13px;
  color: var(--color-body);
  line-height: 1.6;
}
.fur-note,
.fur-warn,
.fur-error,
.fur-notice {
  margin: 6px 0 0;
  font-size: 13px;
  line-height: 1.7;
}
.fur-note {
  color: var(--color-text);
}
.fur-warn {
  color: #d97706;
}
.fur-error {
  color: #e11d48;
  word-break: break-all;
}
.fur-notice.tone-ok {
  color: #16a34a;
}
.fur-notice.tone-warn {
  color: #d97706;
}
.fur-notice.tone-bad {
  color: #e11d48;
}
</style>

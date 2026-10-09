<template>
  <aside class="sidebar">
    <!-- 侧边栏头部 -->
    <div class="sidebar-header">
      <div class="logo">
        <div class="logo-mark">AI</div>
        <div class="logo-meta">
          <span class="logo-text">阿飞飞</span>
          <span class="logo-sub">供应链智能助手</span>
        </div>
      </div>
      <button class="new-chat-btn" @click="$emit('new-chat')">
        <span class="icon">+</span>
        新建对话
      </button>
    </div>

    <!-- 搜索框 -->
    <div class="search-box">
      <input
        type="text"
        v-model="searchKeyword"
        placeholder="搜索会话..."
        class="search-input"
      />
    </div>

    <!-- 会话列表 -->
    <div class="session-list">
      <div class="session-list-header">
        <span>历史记录</span>
      </div>

      <div class="session-items">
        <div
          v-for="session in filteredSessions"
          :key="session.thread_id"
          class="session-item"
          :class="{ active: session.thread_id === currentThreadId }"
          @click="$emit('select-session', session.thread_id)"
        >
          <div class="session-info">
            <span class="session-title">{{ session.title }}</span>
            <span class="session-time">{{ formatTime(session.updated_at) }}</span>
          </div>
          <button
            class="delete-btn"
            @click.stop="handleDelete(session.thread_id)"
            title="删除会话"
          >
            ×
          </button>
        </div>

        <!-- 空状态 -->
        <div v-if="sessions.length === 0" class="empty-state">
          <p>暂无会话记录</p>
          <p class="hint">开始一个新对话吧</p>
        </div>
      </div>
    </div>

    <!-- 侧边栏底部 -->
    <div class="sidebar-footer">
      <div class="footer-info">
        <span class="dot"></span>
        <span class="footer-label">多智能体服务</span>
        <span class="version">v1.0.0</span>
      </div>
    </div>
  </aside>
</template>

<script setup>
import { ref, computed } from 'vue'

// Props 定义
const props = defineProps({
  sessions: {
    type: Array,
    default: () => []
  },
  currentThreadId: {
    type: String,
    default: null
  }
})

// Emits 定义
const emit = defineEmits(['select-session', 'new-chat', 'delete-session'])

// 搜索关键词
const searchKeyword = ref('')

// 过滤后的会话列表
const filteredSessions = computed(() => {
  if (!searchKeyword.value) {
    return props.sessions
  }
  return props.sessions.filter(session =>
    session.title.toLowerCase().includes(searchKeyword.value.toLowerCase())
  )
})

/**
 * 格式化时间
 */
function formatTime(timestamp) {
  if (!timestamp) return ''

  const date = new Date(timestamp)
  const now = new Date()
  const diff = now - date

  // 一天内显示相对时间
  if (diff < 24 * 60 * 60 * 1000) {
    const hours = Math.floor(diff / (60 * 60 * 1000))
    if (hours < 1) {
      const minutes = Math.floor(diff / (60 * 1000))
      return minutes < 1 ? '刚刚' : `${minutes} 分钟前`
    }
    return `${hours} 小时前`
  }

  // 超过一天显示日期
  return `${date.getMonth() + 1}/${date.getDate()}`
}

/**
 * 处理删除会话
 */
function handleDelete(threadId) {
  if (confirm('确定要删除这个会话吗？')) {
    emit('delete-session', threadId)
  }
}
</script>

<style scoped>
.sidebar {
  width: 280px;
  height: 100vh;
  background: #ffffff;
  color: #1e293b;
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  border-right: 1px solid #e2e8f0;
  position: relative;
}

/* 侧边栏头部 */
.sidebar-header {
  padding: 20px 16px;
  border-bottom: 1px solid #f1f5f9;
  background: #fafafa;
}

.logo {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 20px;
}

.logo-icon {
  height: 60px;
  width: auto;
}

.logo-text {
  font-size: 20px;
  font-weight: 700;
  background: linear-gradient(135deg, #0ea5e9 0%, #6366f1 100%);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
  letter-spacing: 0.5px;
}

.new-chat-btn {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 12px 20px;
  background: linear-gradient(135deg, #0ea5e9 0%, #6366f1 100%);
  color: #fff;
  border: none;
  border-radius: 12px;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
  box-shadow: 0 4px 12px rgba(14, 165, 233, 0.3);
  position: relative;
  overflow: hidden;
}

.new-chat-btn:hover {
  transform: translateY(-2px);
  box-shadow: 0 8px 20px rgba(14, 165, 233, 0.4);
}

.new-chat-btn:active {
  transform: translateY(0);
}

.new-chat-btn .icon {
  font-size: 20px;
  font-weight: 300;
}

/* 搜索框 */
.search-box {
  padding: 16px;
}

.search-input {
  width: 100%;
  padding: 12px 16px;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 10px;
  color: #1e293b;
  font-size: 14px;
  outline: none;
  transition: all 0.3s ease;
}

.search-input:focus {
  border-color: #0ea5e9;
  box-shadow: 0 0 0 3px rgba(14, 165, 233, 0.1);
}

.search-input::placeholder {
  color: #94a3b8;
}

/* 会话列表 */
.session-list {
  flex: 1;
  overflow-y: auto;
  padding: 4px 0;
}

.session-list::-webkit-scrollbar {
  width: 4px;
}

.session-list::-webkit-scrollbar-track {
  background: transparent;
}

.session-list::-webkit-scrollbar-thumb {
  background: #cbd5e1;
  border-radius: 2px;
}

.session-list::-webkit-scrollbar-thumb:hover {
  background: #94a3b8;
}

.session-list-header {
  padding: 12px 16px 8px;
  font-size: 12px;
  color: #94a3b8;
  text-transform: uppercase;
  letter-spacing: 1px;
  font-weight: 600;
}

.session-items {
  padding: 0 12px;
}

.session-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 14px;
  margin-bottom: 4px;
  border-radius: 10px;
  cursor: pointer;
  transition: all 0.2s ease;
  border: 1px solid transparent;
}

.session-item:hover {
  background: #f1f5f9;
}

.session-item.active {
  background: #e0f2fe;
  border-color: #7dd3fc;
}

.session-info {
  flex: 1;
  overflow: hidden;
}

.session-title {
  display: block;
  font-size: 14px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  color: #1e293b;
  font-weight: 500;
}

.session-time {
  display: block;
  font-size: 12px;
  color: #94a3b8;
  margin-top: 3px;
}

.session-item.active .session-time {
  color: #0ea5e9;
}

.delete-btn {
  opacity: 0;
  padding: 6px 10px;
  background: #fef2f2;
  border: 1px solid #fecaca;
  border-radius: 6px;
  color: #dc2626;
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
  transition: all 0.2s ease;
}

.session-item:hover .delete-btn {
  opacity: 1;
}

.delete-btn:hover {
  background: #fee2e2;
  border-color: #f87171;
}

/* 空状态 */
.empty-state {
  padding: 60px 24px;
  text-align: center;
  color: #94a3b8;
}

.empty-state p {
  margin-bottom: 10px;
}

.empty-state .hint {
  font-size: 13px;
  color: #cbd5e1;
}

/* 侧边栏底部 */
.sidebar-footer {
  padding: 16px;
  border-top: 1px solid #f1f5f9;
  background: #fafafa;
}

.footer-info {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.version {
  font-size: 12px;
  color: #94a3b8;
  letter-spacing: 0.5px;
}

/* ============================================================
   产品化视觉覆盖（2026-09-29）
   为什么集中放在末尾：同优先级下后写的规则生效，所以这里能覆盖
   上面的既有样式；要回滚只需删掉这一整段，不影响原样式的完整性。
   只动颜色/间距/圆角/阴影，不动布局结构，降低盲改风险。
   ============================================================ */
.sidebar {
  background: linear-gradient(180deg, #0f172a 0%, #121d33 100%);
  border-right: 1px solid rgba(255, 255, 255, 0.06);
}

/* 品牌区：色块标识 + 主标题 + 副标题 */
.logo {
  display: flex;
  align-items: center;
  gap: 10px;
}
.logo-mark {
  width: 34px;
  height: 34px;
  flex: 0 0 34px;
  border-radius: 10px;
  background: linear-gradient(135deg, #0ea5e9 0%, #6366f1 100%);
  color: #fff;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.5px;
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 4px 12px rgba(14, 165, 233, 0.35);
}
.logo-meta {
  display: flex;
  flex-direction: column;
  line-height: 1.25;
}
.logo-text {
  color: #f8fafc;
  font-size: 16px;
  font-weight: 700;
  letter-spacing: 0.5px;
}
.logo-sub {
  color: #7c8aa5;
  font-size: 11px;
  letter-spacing: 0.3px;
}

/* 搜索框：适配深色底 */
.search-input {
  background: rgba(255, 255, 255, 0.06);
  border: 1px solid rgba(255, 255, 255, 0.08);
  color: #e2e8f0;
}
.search-input::placeholder {
  color: #64748b;
}
.search-input:focus {
  border-color: rgba(14, 165, 233, 0.6);
  background: rgba(255, 255, 255, 0.09);
}

/* 历史记录 */
.session-list-header span {
  color: #64748b;
  font-size: 11px;
  letter-spacing: 1px;
}
.session-item {
  border-radius: 10px;
}
.session-item:hover {
  background: rgba(255, 255, 255, 0.06);
}
.session-item.active {
  background: rgba(14, 165, 233, 0.16);
  box-shadow: inset 2px 0 0 #0ea5e9;
}
.session-title {
  color: #cbd5e1;
}
.session-item.active .session-title {
  color: #f1f5f9;
}
.session-time {
  color: #64748b;
}
.empty-state p,
.hint {
  color: #64748b;
}

/* 底部状态条 */
.sidebar-footer {
  background: transparent;
  border-top: 1px solid rgba(255, 255, 255, 0.06);
}
.footer-info {
  gap: 8px;
}
.footer-label {
  color: #64748b;
  font-size: 11px;
  margin-right: auto;
}
.version {
  color: #475569;
}
.dot {
  width: 7px;
  height: 7px;
  flex: 0 0 7px;
  border-radius: 50%;
  background: #22c55e;
  box-shadow: 0 0 0 3px rgba(34, 197, 94, 0.16);
}
</style>

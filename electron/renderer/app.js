// 游戏轮换控制台 - 渲染层逻辑 (作戦指挥台 · 白)
'use strict'

const PHASE_TEXT = {
  idle: '空闲',
  emulator: '正在准备模拟器',
  game: '脚本运行中',
  recovering: '故障恢复中 · 重启模拟器',
  shutting_down: '正在关闭模拟器',
  finished: '本轮已结束',
  stopped: '已手动停止',
  error: '出错 · 详情见日志',
}
const RUNNING_PHASES = new Set(['emulator', 'game', 'recovering', 'shutting_down'])
const STATUS_VERB = {
  pending: '待机', running: '进行中', SUCCESS: '已完成', FAILED: '失败',
  SKIPPED: '已跳过', skipped: '已跳过', STOPPED: '已停止',
  disabled: '未启用', DISABLED: '未启用',
}

let cfg = null
let lastStatus = null
let hasRuntime = true
let busy = false
const queue = new Map() // gid -> {el, sw, verb, num, note}

const $ = (id) => document.getElementById(id)
const logPane = $('log')

// 配置里的名称/路径可能含 < > & , 插模板前转义
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;')
}

function fmtElapsed(sec) {
  sec = Math.max(0, Math.floor(sec))
  const hh = Math.floor(sec / 3600)
  const mm = String(Math.floor(sec % 3600 / 60)).padStart(2, '0')
  const ss = String(sec % 60).padStart(2, '0')
  return hh > 0 ? hh + ':' + mm + ':' + ss : mm + ':' + ss
}

// 本地日期 YYYY-MM-DD (与引擎 datetime.now() 口径一致; toISOString 是 UTC, 东八区凌晨会差一天)
function localDateStr(d) {
  d = d || new Date()
  const p = (n) => String(n).padStart(2, '0')
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate())
}

// ---- 初始化 ----

let inited = false

async function init() {
  let data = null
  try {
    data = await window.api.init()
  } catch (e) {
    log('ERROR', '[GUI] 初始化失败: ' + (e && e.message ? e.message : e))
  }
  // 配置可能正被引擎/主进程原子写入: 读不到就重试, 不要留一个空白队列
  if (!data || !data.config) {
    log('WARN', '[GUI] 配置尚未可读 (可能正在写入), 1s 后重试')
    setTimeout(init, 1000)
    return
  }
  cfg = data.config
  hasRuntime = data.hasRuntime
  lastStatus = data.status
  if (!inited) {          // 重试时不要重复绑定监听器
    inited = true
    if (globalThis.lucide && lucide.createIcons) {
      lucide.createIcons({ attrs: { width: 15, height: 15 } })
    }
    wireButtons()
    wireSettings()
    wireEvents()
    log('INFO', '[GUI] 控制台已就绪' + (hasRuntime ? '' : ' (警告: 缺少 runtime/python 引擎运行时!)'))
  }
  buildQueue()
  renderStatus(lastStatus)
}

function buildQueue() {
  const wrap = $('queue')
  wrap.innerHTML = ''
  queue.clear()
  cfg.rotation.order.forEach((gid, i) => {
    const g = cfg.games[gid]
    if (!g) {  // order 引用了不存在的游戏 (手编配置出错): 跳过而不是白屏
      log('ERROR', '[配置] rotation.order 引用了不存在的游戏: ' + gid)
      return
    }
    const row = document.createElement('div')
    row.className = 'fd-qrow'
    row.dataset.gid = gid
    row.innerHTML = [
      '<span class="fd-grip" role="button" tabindex="0" aria-label="拖动调整轮换顺序" title="按住任意位置上下拖动，调整轮换顺序">⋮⋮</span>',
      '<span class="fd-no">' + String(i + 1).padStart(2, '0') + '</span>',
      '<span class="fd-rail"><span class="fd-dot"></span></span>',
      '<span class="fd-gname">' + esc(g.name || gid) + '</span>',
      '<span class="fd-verb">待机</span>',
      '<span class="fd-num">--</span>',
      '<span class="fd-note">' + esc(g.package || g.note || '') + '</span>',
      '<span class="fd-qact">' +
        '<button data-act="test" title="单独试跑这个游戏">测</button>' +
        '<button data-act="edit" title="编辑配置">改</button>' +
        '<button data-act="del" title="从轮换中删除">删</button>' +
      '</span>',
      '<div class="fd-sw ' + (g.enabled ? 'on' : '') + '" role="switch" aria-checked="' +
        !!g.enabled + '" aria-label="启用' + esc(g.name || gid) + '"></div>',
    ].join('')
    wrap.appendChild(row)
    const sw = row.querySelector('.fd-sw')
    sw.addEventListener('click', () => onToggle(gid, sw))
    row.querySelector('.fd-qact').addEventListener('click', (e) => {
      const btn = e.target.closest('button')
      if (!btn) return
      e.stopPropagation()
      const act = btn.dataset.act
      if (act === 'test') testGame(gid)
      else if (act === 'edit') openGameWizard('edit', gid)
      else if (act === 'del') askDelete(gid, btn)
    })
    wireDrag(row)
    const note = row.querySelector('.fd-note')
    note.dataset.base = g.package || ''
    queue.set(gid, {
      el: row, sw,
      verb: row.querySelector('.fd-verb'),
      num: row.querySelector('.fd-num'),
      note,
      startedAt: '',
    })
  })
}

// ---- 拖动调整轮换顺序（指针拖拽 + FLIP 动画，替代原生 HTML5 拖拽） ----
const DRAG_THRESHOLD = 4      // 超过 4px 才算拖动，避免误触
let drag = null               // {row, startY, comp, active, rows}

function wireDrag(row) {
  const grip = row.querySelector('.fd-grip')

  // 整行都能按住拖（开关/行内操作按钮除外），把手只是提示
  row.addEventListener('pointerdown', (e) => {
    if (busy || e.button !== 0) return
    if (e.target.closest('.fd-sw') || e.target.closest('.fd-qact')) return
    drag = { row, startY: e.clientY, comp: 0, active: false, rows: null }
    document.addEventListener('pointermove', onDragMove, { passive: false })
    document.addEventListener('pointerup', endDrag, { once: true })
    document.addEventListener('pointercancel', endDrag, { once: true })
  })

  // 键盘也能调（Tab 聚焦把手后 ↑/↓）
  grip.addEventListener('keydown', (e) => {
    if (busy) return
    if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return
    e.preventDefault()
    const sib = e.key === 'ArrowUp' ? row.previousElementSibling : row.nextElementSibling
    if (!sib) return
    if (e.key === 'ArrowUp') row.parentNode.insertBefore(row, sib)
    else row.parentNode.insertBefore(sib, row)
    grip.focus()
    commitOrder()
  })
}

function onDragMove(e) {
  const s = drag
  if (!s) return
  const dy = e.clientY - s.startY

  if (!s.active) {
    if (Math.abs(dy) < DRAG_THRESHOLD) return
    s.active = true
    s.rows = [...$('queue').children]
    s.row.classList.add('fd-dragging')
    document.body.classList.add('fd-noselect')
  }
  e.preventDefault()

  // 被拖动的那行跟着指针走
  s.row.style.transform = 'translateY(' + (dy + s.comp) + 'px)'

  // 指针越过了谁的中线，就插到谁前面
  const others = s.rows.filter((r) => r !== s.row)
  let idx = others.findIndex((r) => {
    const b = r.getBoundingClientRect()
    return e.clientY < b.top + b.height / 2
  })
  if (idx === -1) idx = others.length
  const before = others[idx] || null

  if (s.row.nextElementSibling !== before) {
    const off0 = s.row.offsetTop
    const tops = others.map((r) => r.offsetTop)
    $('queue').insertBefore(s.row, before)
    // 补偿位移：DOM 换位后视觉位置不能跳
    s.comp += off0 - s.row.offsetTop
    s.row.style.transform = 'translateY(' + (dy + s.comp) + 'px)'
    // FLIP：其余行从旧位置滑到新位置
    others.forEach((r, i) => {
      const d = tops[i] - r.offsetTop
      if (!d) return
      r.style.transition = 'none'
      r.style.transform = 'translateY(' + d + 'px)'
      requestAnimationFrame(() => {
        r.style.transition = 'transform .16s cubic-bezier(.2,.7,.3,1)'
        r.style.transform = ''
      })
    })
  }
}

function endDrag() {
  document.removeEventListener('pointermove', onDragMove)
  document.body.classList.remove('fd-noselect')
  const s = drag
  drag = null
  if (!s || !s.active) return

  const row = s.row
  row.classList.remove('fd-dragging')
  // 落位：从当前视觉位置滑进新槽位
  row.style.transition = 'transform .18s cubic-bezier(.2,.7,.3,1)'
  row.style.transform = 'translateY(0)'
  const done = () => {
    row.style.transition = ''
    row.style.transform = ''
    row.removeEventListener('transitionend', done)
  }
  row.addEventListener('transitionend', done)
  setTimeout(done, 300)

  s.rows.forEach((r) => {
    if (r === row) return
    r.style.transition = ''
    r.style.transform = ''
  })
  commitOrder()
}

function renumber() {
  const nos = $('queue').querySelectorAll('.fd-qrow .fd-no')
  nos.forEach((el, i) => { el.textContent = String(i + 1).padStart(2, '0') })
}

async function commitOrder() {
  const order = [...$('queue').querySelectorAll('.fd-qrow')].map((r) => r.dataset.gid)
  if (!order.length || order.join() === cfg.rotation.order.join()) return
  const res = await window.api.setOrder(order)
  if (!res.ok) {
    log('ERROR', '[配置] 顺序保存失败: ' + (res.msg || ''))
    const data = await window.api.init()
    if (data && data.config) cfg = data.config
    buildQueue()
    renderStatus(lastStatus)
    return
  }
  cfg.rotation.order = order
  renumber()
  log('INFO', '[配置] 轮换顺序: ' + order.map((g) => cfg.games[g].name).join(' → '))
}

// ---- 交互 ----

async function onToggle(gid, sw) {
  if (busy) { syncSwitches(); return }
  const on = !sw.classList.contains('on')
  const res = await window.api.setEnabled(gid, on)
  if (res.ok) {
    cfg.games[gid].enabled = on
    syncSwitches()
    const names = cfg.rotation.order
      .filter((g) => cfg.games[g].enabled)
      .map((g) => cfg.games[g].name)
    log('INFO', '[配置] 已启用: ' + (names.length ? names.join('、') : '(无)'))
  }
}

function syncSwitches() {
  for (const [gid, c] of queue) {
    c.sw.classList.toggle('on', !!cfg.games[gid].enabled)
    c.sw.classList.toggle('locked', busy)
  }
  const sOn = $('sw-schedule').classList.contains('on')
  const nOn = $('sw-notify').classList.contains('on')
  $('sw-mute').classList.toggle('locked', busy)
  $('sw-schedule').classList.toggle('locked', busy)
  $('sw-notify').classList.toggle('locked', busy)
  $('sched-time').disabled = busy
  $('sched-autostart') && ($('sched-autostart').disabled = busy)
  // 保持设置开关与配置一致 (轮换中引擎不读它, 但避免误导)
  $('sw-schedule').classList.toggle('on', sOn)
  $('sw-notify').classList.toggle('on', nOn)
}

function wireButtons() {
  $('btn-start').addEventListener('click', async () => {
    if (busy) return
    busy = true
    $('btn-start').disabled = true
    $('btn-stop').disabled = false
    const res = await window.api.start()
    if (!res.ok) {
      log('ERROR', '[GUI] ' + res.msg)
      busy = false
      $('btn-start').disabled = false
      $('btn-stop').disabled = true
    }
  })
  $('btn-stop').addEventListener('click', async () => {
    if (!busy) return
    await window.api.stop()
  })
  $('btn-check').addEventListener('click', () => runTool('doctor-fix'))
  $('btn-detect').addEventListener('click', () => runTool('detect-packages'))
  $('btn-history').addEventListener('click', openHistory)
  $('btn-hist-close').addEventListener('click', closeHistory)
  $('hist-modal').addEventListener('click', (e) => {
    if (e.target === $('hist-modal')) closeHistory()
  })
  $('btn-add-game').addEventListener('click', () => openGameWizard('add', ''))
  $('btn-game-close').addEventListener('click', closeGameWizard)
  $('btn-game-save').addEventListener('click', saveGameWizard)
  $('game-modal').addEventListener('click', (e) => {
    if (e.target === $('game-modal')) closeGameWizard()
  })
  for (const id of ['btn-logs', 'btn-logs-actions']) {
    const el = $(id)
    if (el) el.addEventListener('click', () => window.api.openLogs())
  }
  $('btn-clear').addEventListener('click', () => { logPane.innerHTML = '' })
  $('btn-emu-start').addEventListener('click', async () => {
    log('INFO', '[GUI] 正在启动模拟器...')
    await window.api.emuAction('start')
  })
  $('btn-emu-stop').addEventListener('click', async () => {
    if (busy) return // 轮换中禁止关闭模拟器
    log('INFO', '[GUI] 正在关闭模拟器...')
    await window.api.emuAction('stop')
  })
}

async function runTool(name) {
  if (busy) return
  const res = await window.api.tool(name)
  if (!res.ok) log('ERROR', '[GUI] ' + res.msg)
}

// ---- 设置 (每日定时 / 完成推送 / 静音) ----

function wireSettings() {
  const s = cfg.schedule || {}
  const n = cfg.notify || {}
  $('sw-schedule').classList.toggle('on', !!s.enabled)
  $('sched-time').value = s.time || '03:30'
  $('sched-autostart').checked = !!s.autostart
  $('sw-notify').classList.toggle('on', !!n.enabled)
  $('notify-channel').value = n.channel || 'serverchan'
  $('notify-token').value = n.token || ''
  $('notify-chat').value = n.chat_id || ''
  $('notify-chat').style.display =
    $('notify-channel').value === 'telegram' ? '' : 'none'
  $('sw-mute').classList.toggle('on', !!(cfg.rotation || {}).mute_emulator)
  $('sw-parallel').classList.toggle('on',
    (cfg.rotation || {}).parallel !== false)
  updateSchedHint()

  const bindSwitch = (id) => {
    $(id).addEventListener('click', () => {
      $(id).classList.toggle('on')
      saveSettings()
    })
  }
  bindSwitch('sw-schedule')
  bindSwitch('sw-notify')
  bindSwitch('sw-mute')
  bindSwitch('sw-parallel')
  for (const id of ['sched-time', 'sched-autostart', 'notify-channel',
    'notify-token', 'notify-chat']) {
    $(id).addEventListener('change', () => {
      $('notify-chat').style.display =
        $('notify-channel').value === 'telegram' ? '' : 'none'
      saveSettings()
    })
  }
  $('btn-push-test').addEventListener('click', async () => {
    await saveSettings()
    await runTool('push-test')
  })
}

async function saveSettings() {
  const res = await window.api.saveSettings({
    schedule: {
      enabled: $('sw-schedule').classList.contains('on'),
      time: $('sched-time').value || '03:30',
      autostart: $('sched-autostart').checked,
      justEnabled: $('sw-schedule').classList.contains('on') &&
        !(cfg.schedule || {}).enabled, // 刚开启: 当日不再补跑
    },
    notify: {
      enabled: $('sw-notify').classList.contains('on'),
      channel: $('notify-channel').value,
      token: $('notify-token').value.trim(),
      chat_id: $('notify-chat').value.trim(),
    },
    rotation_mute: $('sw-mute').classList.contains('on'),
    rotation_parallel: $('sw-parallel').classList.contains('on'),
  })
  if (res.ok) {
    cfg.schedule = Object.assign(cfg.schedule || {}, {
      enabled: $('sw-schedule').classList.contains('on'),
      time: $('sched-time').value || '03:30',
      autostart: $('sched-autostart').checked,
    })
    cfg.notify = Object.assign(cfg.notify || {}, {
      enabled: $('sw-notify').classList.contains('on'),
      channel: $('notify-channel').value,
      token: $('notify-token').value.trim(),
      chat_id: $('notify-chat').value.trim(),
    })
    cfg.rotation = Object.assign(cfg.rotation || {}, {
      mute_emulator: $('sw-mute').classList.contains('on'),
      parallel: $('sw-parallel').classList.contains('on'),
    })
    updateSchedHint()
  } else {
    log('ERROR', '[GUI] 设置保存失败: ' + (res.msg || ''))
  }
}

function updateSchedHint() {
  const s = cfg.schedule || {}
  const row = $('sched-time')
  if (!s.enabled) {
    row.title = '开启后每天到点自动开始轮换'
  } else {
    row.title = '每天 ' + (s.time || '03:30') + ' 自动开始轮换'
  }
}

// ---- 事件 (主进程推送) ----

function wireEvents() {
  window.api.on('status', (st) => { lastStatus = st; renderStatus(st) })
  window.api.on('emu', ({ state, color }) => {
    $('emu-label').textContent = '模拟器: ' + state
    const dot = $('emu-dot')
    dot.className = 'fd-lamp' +
      (color === '#1f9d55' ? ' on' : color === '#c07f00' ? ' warn' : ' off')
  })
  window.api.on('log', (line) => log('INFO', line))
  window.api.on('watch', (line) => log('WATCH', line))
  window.api.on('tool-done', async () => {
    const data = await window.api.init()
    cfg = data.config
    buildQueue()
    renderStatus(lastStatus)
  })
  window.api.on('engine-exit', (code) => {
    log(code === 0 ? 'INFO' : 'WARN', '[GUI] 轮换引擎退出 (退出码 ' + code + ')')
  })
}

// ---- 状态渲染 ----

function renderStatus(st) {
  try {
  st = st || {}
  const phase = st.phase || 'idle'
  busy = engineAlive(st)

  let games = st.games || []
  if (!games.length && cfg) {
    games = cfg.rotation.order
      .filter((gid) => cfg.games[gid])  // 跳过幽灵条目
      .map((gid) => ({
        id: gid, name: cfg.games[gid].name,
        status: cfg.games[gid].enabled ? 'pending' : 'disabled',
        tries: 0, elapsed: 0,
      }))
  }
  const running = games.find((g) => g.status === 'running')
  const maxRetry = (cfg ? cfg.rotation.max_retries : 2) + 1

  // 时钟
  const label = $('phase')
  if (running) {
    label.textContent = '当前任务 · ' + running.name
  } else {
    label.textContent = PHASE_TEXT[phase] || phase
  }
  if (!running) $('clock-num').textContent = '00:00:00'
  $('fd-attempt').textContent = running
    ? '第 ' + (running.tries || 1) + '/' + maxRetry + ' 次尝试'
    : ''
  // 大数字状态机: 运行中=当前任务计时 / 空闲有定时=倒计时 / 结束=上轮总用时
  var clockState = { mode: 'idle', base: 0, gid: '' }
  if (running) {
    clockState.mode = 'run'
    clockState.gid = running.id
    const ts = Date.parse((running.started_at || '').replace(' ', 'T'))
    clockState.base = isNaN(ts) ? Date.now() : ts
  } else if ((phase === 'finished' || phase === 'stopped')) {
    const total = games.reduce((a, g) => a + (g.elapsed || 0), 0)
    clockState.mode = 'total'
    clockState.base = total
  } else if (phase === 'idle' && cfg.schedule && cfg.schedule.enabled && cfg.schedule.time) {
    const tgt = nextRunTarget(cfg.schedule)
    clockState.mode = 'countdown'
    clockState.base = tgt.getTime()
    clockState.dayLabel = sameDay(tgt) ? '今天 ' : '明天 '
    $('fd-attempt').textContent = '距自动开始 · ' + clockState.dayLabel + cfg.schedule.time
  } else {
    clockState.mode = 'none'
    $('clock-num').textContent = '--:--:--'
    $('fd-attempt').textContent = ''
  }
  window.__fdClock = clockState
  if (clockState.mode === 'run') {
    $('clock-num').textContent = fmtElapsed((Date.now() - clockState.base) / 1000)
  } else if (clockState.mode === 'countdown') {
    $('clock-num').textContent = fmtElapsed(Math.max(0, (clockState.base - Date.now()) / 1000))
  } else if (clockState.mode === 'total') {
    $('clock-num').textContent = clockState.base > 0 ? fmtElapsed(clockState.base) : '--:--:--'
  } else if (clockState.mode === 'none') {
    $('clock-num').textContent = '--:--:--'
  }
  const dotEl = document.querySelector('.fd-clock .fd-live')
  dotEl.className = 'fd-live' + (busy ? '' : (phase === 'finished' ? ' ok' : ' quiet'))
  const what = $('fd-what')
  if (busy) {
    what.innerHTML = '轮换进行中，请勿操作模拟器窗口'
  } else if (phase === 'finished' && st.last_result) {
    what.innerHTML = '上次结果: <b>' + st.last_result + '</b>'
  } else if (phase === 'stopped') {
    what.innerHTML = '已手动停止 · 模拟器保持运行'
  } else if (phase === 'error') {
    what.innerHTML = '出错了 · 日志里有原因'
  } else {
    what.textContent = '到点自动开始，或手动开始一轮'
  }

  // 队列
  let done = 0
  for (const entry of games) {
    const c = queue.get(entry.id)
    if (!c) continue
    const status = entry.status || 'pending'
    if (status !== 'pending' && status !== 'running') done++
    const verb = STATUS_VERB[status] || status
    c.verb.textContent = verb
    let numTxt = '--'
    let noteTxt = c.note.dataset.base || ''
    if (status === 'running') {
      c.startedAt = entry.started_at || ''
      const ts = Date.parse((entry.started_at || '').replace(' ', 'T'))
      numTxt = isNaN(ts) ? '00:00' : fmtElapsed((Date.now() - ts) / 1000)
      noteTxt = '第 ' + (entry.tries || 1) + '/' + maxRetry + ' 次尝试'
    } else if (entry.elapsed) {
      numTxt = fmtElapsed(entry.elapsed)
      if (entry.tries > 1) noteTxt = '共 ' + entry.tries + ' 次尝试'
    }
    c.num.textContent = numTxt
    c.note.textContent = noteTxt
    c.el.className = 'fd-qrow' +
      (status === 'running' ? ' fd-run' : '') +
      (status === 'SUCCESS' ? ' fd-done' : '') +
      (status === 'FAILED' ? ' fd-bad' : '') +
      (status === 'disabled' ? ' fd-off' : '') +
      (busy ? ' fd-locked' : '') +
      (drag && drag.row === c.el ? ' fd-dragging' : '')
  }
  $('progress').style.width = (games.length ? done / games.length * 100 : 0) + '%'
  $('progress-track').classList.toggle('busy', busy)
  $('prog-text').textContent = games.length ? done + ' / ' + games.length : '--'
  $('started').textContent = st.started_at
    ? st.started_at.slice(11) : '--'

  // 定时下次
  const s = (cfg.schedule || {})
  if (s.enabled && s.time) {
    const nowHM = new Date().toTimeString().slice(0, 5)
    const today = localDateStr()
    $('sched-next').textContent =
      (nowHM < s.time && s.last_run_date !== today ? '今天 ' : '明天 ') + s.time
  } else {
    $('sched-next').textContent = '未启用'
  }

  // 按钮/开关
  // 按钮: busy 含外部引擎(状态机判定), 停止键在 busy 时必须可用
  $('btn-start').disabled = busy || !hasRuntime
  $('btn-stop').disabled = !busy
  $('btn-emu-start').disabled = busy
  $('btn-emu-stop').disabled = busy
  syncSwitches()
  const lastRes = st.last_result ? '上次结果: ' + st.last_result + ' · ' : ''
  $('foot').textContent = busy
    ? '运行中 · 请勿操作模拟器窗口 (最小化到托盘不影响轮换)'
    : lastRes + '空闲 · 可开始轮换 | ✕ 最小化到托盘'
  } catch (e) {
    log('ERROR', '[渲染] renderStatus 异常: ' + (e && e.message ? e.message : e) +
      ' @ ' + (e && e.stack ? String(e.stack).split('\n')[1] : ''))
  }
}

function engineAlive(st) {
  if (!RUNNING_PHASES.has(st.phase)) return false
  // 心跳过期视为引擎已死, 避免状态文件残留导致 UI 永久卡在"运行中"。
  // 引擎侧保证 <60s 一次心跳(wait_app/探针/UIA 段已补), 单次阻塞上限 90s, 故取 150s。
  const ts = Date.parse((st.updated_at || '').replace(' ', 'T'))
  if (isNaN(ts)) return true
  return Date.now() - ts < 150000
}

// 实时计时: 运行中=任务计时 / 空闲定时=倒计时
// 诊断行: 每 15s 报告渲染层视角的状态, 便于远程定位 UI 问题
setInterval(() => {
  const st = lastStatus
  if (!st) return
  const age = Math.round((Date.now() - Date.parse((st.updated_at || '').replace(' ', 'T'))) / 1000)
  log('MUTED', '[diag] busy=' + busy + ' phase=' + st.phase +
    ' 状态年龄=' + age + 's games=' + (st.games || []).length)
}, 15000)

function nextRunTarget(s) {
  const now = new Date()
  const todayStr = localDateStr(now)
  const parts = (s.time || '03:30').split(':')
  const t = new Date(now.getFullYear(), now.getMonth(), now.getDate(),
    Number(parts[0]) || 0, Number(parts[1]) || 0)
  if ((s.last_run_date || '') === todayStr) t.setDate(t.getDate() + 1)
  else if (now >= t) t.setTime(now.getTime()) // 当日未跑且已过点: 调度器马上补跑
  return t
}

function sameDay(d) {
  const now = new Date()
  return d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() && d.getDate() === now.getDate()
}

setInterval(() => {
  const cs = window.__fdClock
  if (!cs) return
  const clock = $('clock-num')
  if (!clock) return
  if (cs.mode === 'run') {
    clock.textContent = fmtElapsed((Date.now() - cs.base) / 1000)
    const c = queue.get(cs.gid)
    if (c) c.num.textContent = fmtElapsed((Date.now() - cs.base) / 1000)
  } else if (cs.mode === 'countdown') {
    clock.textContent = fmtElapsed(Math.max(0, (cs.base - Date.now()) / 1000))
  }
}, 1000)

// ---- 添加/编辑游戏向导 ----

// 4 个模板 = 引擎的 3 种 runner (alas 按 mode 分两种)
const GAME_TEMPLATES = {
  marker: {
    label: '通用脚本',
    desc: 'MFAAvalonia/MAA 这类自己跑的脚本：监控它写的日志，出现成功标记就算完成',
    type: 'marker',
    fields: ['name', 'exe', 'args', 'log_file', 'package', 'package_keywords',
      'timeout_min', 'success_markers', 'fail_markers'],
    req: ['name', 'exe', 'log_file', 'timeout_min'],
  },
  'alas-ui': {
    label: 'ALAS 界面',
    desc: 'AzurLaneAutoScript 的 Alas.exe 窗口模式：能看到画面，也能随时手动干预',
    type: 'alas',
    fields: ['name', 'ui_exe', 'log_dir', 'dir', 'package', 'package_keywords',
      'timeout_min'],
    req: ['name', 'ui_exe', 'timeout_min'],
    labels: { dir: 'ALAS 根目录 (可选)', log_dir: '日志目录 (可选)' },
  },
  'alas-headless': {
    label: 'ALAS 无头',
    desc: '不打开界面，直接用 ALAS 自带的 python 后台跑 alas.py (最省资源)',
    type: 'alas',
    fields: ['name', 'dir', 'python', 'entry', 'package', 'package_keywords',
      'timeout_min'],
    req: ['name', 'dir', 'python', 'entry', 'timeout_min'],
  },
  mxu: {
    label: 'MaaEnd / PC 端',
    desc: 'MaaEnd 这类 Tauri 桌面程序：启动后自动点「开始任务」，游戏本身可自行启动',
    type: 'mxu',
    fields: ['name', 'exe', 'dir', 'start_button', 'log_dir', 'timeout_min',
      'success_markers', 'fail_markers'],
    req: ['name', 'exe', 'timeout_min'],
    labels: { dir: '程序目录', log_dir: '日志目录' },
  },
}

// 字段元信息: 标签/占位/是否文件·目录 (必填由各模板的 req 决定)
const FIELD_META = {
  name: { label: '显示名称', ph: '例: 崩坏：星穹铁道' },
  exe: { label: '脚本程序', ph: '选择脚本 exe', pick: 'file',
    filters: [{ name: '程序', extensions: ['exe'] }] },
  args: { label: '启动参数', ph: '可留空，如 --autostart' },
  log_file: { label: '日志文件', ph: '脚本写出的日志路径', pick: 'file',
    hint: '跨天滚动可用 {today} 占位，例: logs/log-{today}.log' },
  package: { label: '游戏包名', ph: '可留空，用「包名识别」自动填' },
  package_keywords: { label: '包名关键词', ph: '逗号分隔，如 arknights,azurlane',
    hint: '包名识别时按这些关键词匹配' },
  timeout_min: { label: '硬超时 (分钟)', ph: '如 60', num: true },
  success_markers: { label: '成功标记', ph: '每行一个，日志里出现即完成',
    area: true, hint: '例: 任务已全部完成' },
  fail_markers: { label: '失败标记', ph: '每行一个，出现即判定失败', area: true },
  ui_exe: { label: 'ALAS 界面程序', ph: '选择 Alas.exe', pick: 'file',
    filters: [{ name: '程序', extensions: ['exe'] }] },
  dir: { label: 'ALAS 目录', ph: 'AzurLaneAutoScript 根目录', pick: 'dir' },
  log_dir: { label: '日志目录', ph: '留空则用 <ALAS目录>/log', pick: 'dir' },
  python: { label: 'ALAS Python', ph: 'toolkit\\python.exe', pick: 'file',
    filters: [{ name: '程序', extensions: ['exe'] }] },
  entry: { label: '入口脚本', ph: 'alas.py', pick: 'file',
    filters: [{ name: 'Python', extensions: ['py'] }] },
  start_button: { label: '开始按钮文字', ph: '开始任务',
    hint: 'MaaEnd 界面上那个开始按钮的无障碍名称' },
}

// 数值/数组型字段的存储转换
const NUM_FIELDS = new Set(['timeout_min'])
const LIST_FIELDS = new Set(['success_markers', 'fail_markers', 'package_keywords'])

let gameDraft = null // {mode:'add'|'edit', gid, kind, data}

// 新建时的建议默认值 (其余默认值由主进程写入时补齐)
const KIND_TIMEOUT = { marker: 60, 'alas-ui': 120, 'alas-headless': 120, mxu: 90 }

function openGameWizard(mode, gid) {
  if (busy) { log('WARN', '[配置] 轮换运行中, 不能添加或编辑游戏'); return }
  const src = mode === 'edit' && gid && cfg.games[gid] ? cfg.games[gid] : {}
  const kind = mode === 'edit' ? kindOf(src) : 'marker'
  gameDraft = {
    mode,
    gid: gid || '',
    kind,
    data: Object.assign(
      { timeout_min: KIND_TIMEOUT[kind] },
      JSON.parse(JSON.stringify(src))),
  }
  $('game-modal-title').textContent = mode === 'edit' ? '编辑游戏' : '添加游戏'
  $('game-err').textContent = ''
  renderWizard()
  $('game-modal').classList.remove('hidden')
}

function closeGameWizard() {
  $('game-modal').classList.add('hidden')
  gameDraft = null
}

function kindOf(g) {
  if (!g) return 'marker'
  if (g.type === 'mxu') return 'mxu'
  if (g.type === 'alas') return g.mode === 'ui' ? 'alas-ui' : 'alas-headless'
  return 'marker'
}

function renderWizard() {
  const body = $('game-body')
  const d = gameDraft
  if (!d) return
  const tpl = GAME_TEMPLATES[d.kind]
  const head = [
    '<p class="game-step">第 1 步 · 选择脚本类型（决定轮换器怎么判定它跑完了）</p>',
    '<div class="game-types">',
    Object.keys(GAME_TEMPLATES).map((k) => {
      const t = GAME_TEMPLATES[k]
      return '<button class="game-type' + (k === d.kind ? ' sel' : '') +
        '" data-kind="' + k + '"><b>' + t.label + '</b><span>' + t.desc + '</span></button>'
    }).join(''),
    '</div>',
  ]
  if (d.mode === 'add') {
    head.push(
      '<p class="game-step" style="margin-top:14px">第 2 步 · 填路径与判定标记</p>')
  } else {
    head.push(
      '<p class="game-step" style="margin-top:14px">配置项</p>')
  }
  head.push('<div class="game-form">')
  if (d.mode === 'add') {
    head.push(fieldHtml('游戏 id (英文, 用作配置键)', 'gid', d.gid,
      { ph: '例: starrail', required: true }))
  }
  for (const f of tpl.fields) {
    const meta = FIELD_META[f]
    const label = (tpl.labels && tpl.labels[f]) || meta.label
    head.push(fieldHtml(label, f, d.data[f], meta))
  }
  head.push('</div>')
  body.innerHTML = head.join('')

  // 类型切换
  for (const b of body.querySelectorAll('.game-type')) {
    b.addEventListener('click', () => {
      const k = b.dataset.kind
      if (k === d.kind) return
      // 换类型: 保留各模板共有的值, 丢掉上一类型独有的字段
      const keep = {}
      for (const shared of ['name', 'package', 'package_keywords', 'timeout_min']) {
        const v = readField(shared)
        if (v) keep[shared] = v
      }
      if (!keep.timeout_min) keep.timeout_min = KIND_TIMEOUT[k]
      if (d.mode === 'add') d.gid = readField('gid') || d.gid
      d.kind = k
      d.data = Object.assign({}, keep)
      renderWizard()
    })
  }
  // 文件/目录选择
  for (const btn of body.querySelectorAll('button[data-pick]')) {
    btn.addEventListener('click', async () => {
      const f = btn.dataset.pick
      const meta = FIELD_META[f] || {}
      const input = body.querySelector('[data-field="' + f + '"]')
      const p = meta.pick === 'dir'
        ? await window.api.chooseDir()
        : await window.api.chooseFile(meta.filters)
      if (p) input.value = p
    })
  }
}

function fieldHtml(label, key, val, meta) {
  meta = meta || {}
  const required = meta.required ||
    (GAME_TEMPLATES[gameDraft.kind].req || []).includes(key)
  const id = 'data-field="' + key + '"'
  let input
  if (meta.area) {
    input = '<textarea ' + id + ' placeholder="' + esc(meta.ph || '') + '">' +
      esc(listToText(val)) + '</textarea>'
  } else if (meta.pick) {
    input = '<div class="row"><input ' + id + ' value="' + esc(val == null ? '' : val) +
      '" placeholder="' + esc(meta.ph || '') + '" spellcheck="false" />' +
      '<button type="button" data-pick="' + key + '">选择</button></div>'
  } else {
    input = '<input ' + id + ' value="' + esc(val == null ? '' : val) +
      '" placeholder="' + esc(meta.ph || '') + '" spellcheck="false"' +
      (meta.num ? ' inputmode="numeric"' : '') + ' />'
  }
  return '<div class="game-field" data-key="' + key + '">' +
    '<label>' + esc(label) + (required ? '<b>*</b>' : '') + '</label>' +
    input + (meta.hint ? '<div class="hint">' + esc(meta.hint) + '</div>' : '') +
    '</div>'
}

function listToText(v) {
  if (Array.isArray(v)) return v.join('\n')
  return v == null ? '' : v
}

// 数组字段: 文本域按行拆, 单行输入按逗号拆
function textToList(key, raw) {
  const meta = FIELD_META[key] || {}
  const parts = meta.area ? raw.split('\n') : raw.split(',')
  return parts.map((s) => s.trim()).filter(Boolean)
}

function readField(key) {
  const el = $('game-body').querySelector('[data-field="' + key + '"]')
  return el ? el.value : ''
}

// 收集表单 -> 游戏对象 (只留引擎认识的字段)
function collectGame() {
  const tpl = GAME_TEMPLATES[gameDraft.kind]
  const data = Object.assign({}, gameDraft.data)
  data.type = tpl.type
  if (gameDraft.kind === 'alas-ui') data.mode = 'ui'
  else if (gameDraft.kind === 'alas-headless') data.mode = 'headless'
  if (gameDraft.kind === 'mxu') {
    data.needs_emulator = false
    data.launch_game = false
  }
  for (const f of tpl.fields) {
    const raw = readField(f).trim()
    let v = raw
    if (NUM_FIELDS.has(f)) v = Number(raw) || 0
    else if (LIST_FIELDS.has(f)) v = textToList(f, raw)
    if (v === '' || (Array.isArray(v) && !v.length)) delete data[f]
    else data[f] = v
  }
  if (!data.success_markers) delete data.success_markers
  if (!data.fail_markers) delete data.fail_markers
  if (gameDraft.mode === 'add') data.enabled = true
  return data
}

function showFieldError(errs) {
  for (const el of $('game-body').querySelectorAll('.game-field')) {
    el.classList.remove('err')
  }
  // 错误串形如 "<gid>: 文件不存在 exe=...", 尝试定位到字段
  for (const e of errs) {
    for (const key of Object.keys(FIELD_META)) {
      if (e.includes(' ' + key) || e.includes(key + '=') || e.endsWith(key)) {
        const el = $('game-body').querySelector('[data-key="' + key + '"]')
        if (el) el.classList.add('err')
      }
    }
  }
}

async function saveGameWizard() {
  if (!gameDraft) return
  const gid = gameDraft.mode === 'edit'
    ? gameDraft.gid
    : (readField('gid') || '').trim()
  const data = collectGame()
  if (!gid) {
    $('game-err').textContent = '请填写游戏 id'
    return
  }
  const errs = localValidate(gid, data)
  if (errs.length) {
    $('game-err').textContent = errs[0]
    showFieldError(errs)
    return
  }
  $('btn-game-save').disabled = true
  try {
    const res = gameDraft.mode === 'edit'
      ? await window.api.updateGame(gid, data)
      : await window.api.addGame(gid, data)
    if (!res.ok) {
      $('game-err').textContent = res.msg || '保存失败'
      if (res.errs) showFieldError(res.errs)
      log('ERROR', '[配置] ' + (res.msg || '保存失败'))
      return
    }
    log('INFO', '[配置] 已' + (gameDraft.mode === 'edit' ? '更新: ' : '添加: ') +
      (data.name || gid) + ' (' + gid + ')')
    closeGameWizard()
    await reloadConfig()
  } finally {
    $('btn-game-save').disabled = false
  }
}

// 本地最小校验 (保存前就拦住明显错误; 引擎侧 validate_game 会在运行时再校验)
function localValidate(gid, data) {
  const errs = []
  if (!/^[\w.-]+$/.test(gid)) errs.push('游戏 id 只能含字母/数字/_ . -')
  if (gameDraft.mode === 'add' && cfg.games[gid]) errs.push('游戏 id 已存在: ' + gid)
  if (!data.name) errs.push('请填写显示名称')
  for (const f of GAME_TEMPLATES[gameDraft.kind].req) {
    if (!data[f]) errs.push('请填写' + FIELD_META[f].label)
  }
  if (!(Number(data.timeout_min) > 0)) errs.push('硬超时必须是大于 0 的分钟数')
  return errs
}

async function reloadConfig() {
  const data = await window.api.init()
  if (data && data.config) cfg = data.config
  buildQueue()
  renderStatus(lastStatus)
}

async function askDelete(gid, anchor) {
  if (busy) { log('WARN', '[配置] 轮换运行中, 不能删除游戏'); return }
  const g = cfg.games[gid] || {}
  const ok = await confirmInline(anchor, '删除「' + (g.name || gid) + '」?')
  if (!ok) return
  const res = await window.api.removeGame(gid)
  if (!res.ok) {
    log('ERROR', '[配置] 删除失败: ' + (res.msg || ''))
    return
  }
  log('INFO', '[配置] 已删除: ' + (g.name || gid))
  await reloadConfig()
}

// 就地二次确认: 按钮先变"确认?"再变"删除", 3 秒无操作复原
function confirmInline(btn, _text) {
  return new Promise((resolve) => {
    const old = btn.textContent
    btn.textContent = '确定?'
    btn.classList.add('danger')
    let done = false
    const finish = (v) => {
      if (done) return
      done = true
      btn.textContent = old
      btn.classList.remove('danger')
      btn.removeEventListener('click', onClick, true)
      clearTimeout(t)
      resolve(v)
    }
    const onClick = (e) => {
      e.stopPropagation()
      e.preventDefault()
      finish(true)
    }
    btn.addEventListener('click', onClick, true)
    const t = setTimeout(() => finish(false), 3000)
  })
}

async function testGame(gid) {
  if (busy) { log('WARN', '[配置] 轮换运行中, 不能单测'); return }
  const g = cfg.games[gid] || {}
  log('INFO', '[配置] 开始单测: ' + (g.name || gid) + ' (会真实启动脚本/模拟器)')
  const res = await window.api.testGame(gid)
  if (!res.ok) log('ERROR', '[配置] ' + (res.msg || '单测启动失败'))
}

// ---- 历史记录 ----

async function openHistory() {
  const rows = await window.api.getHistory()
  const list = $('hist-list')
  list.innerHTML = ''
  if (!rows.length) {
    list.innerHTML = '<div class="hist-empty">暂无历史记录，跑完一轮后这里会出现汇总</div>'
  }
  for (const r of rows) {
    const item = document.createElement('div')
    item.className = 'hist-item'
    const line1 = document.createElement('div')
    line1.className = 'fd-ht'
    const t = document.createElement('span')
    t.textContent = r.time || ''
    const trig = document.createElement('span')
    trig.className = 'fd-tag' + (r.trigger === 'schedule' ? '' : ' man')
    trig.textContent = r.trigger === 'schedule' ? '定时' : '手动'
    line1.appendChild(t)
    line1.appendChild(trig)
    item.appendChild(line1)
    const res = document.createElement('div')
    res.className = 'fd-hd'
    for (const g of r.results || []) {
      const span = document.createElement('span')
      const okish = g.status === 'SUCCESS'
      const bad = g.status === 'FAILED'
      span.className = okish ? 'g' : (bad ? 'r' : 'f')
      span.textContent = g.name + ' ' + (STATUS_VERB[g.status] || g.status) +
        (g.elapsed ? ' ' + Math.round(g.elapsed / 60) + 'm' : '')
      res.appendChild(span)
    }
    item.appendChild(res)
    list.appendChild(item)
  }
  $('hist-modal').classList.remove('hidden')
}

function closeHistory() {
  $('hist-modal').classList.add('hidden')
}

// ---- 日志 ----

function log(level, msg) {
  const tag = pickTag(level, msg)
  // 用户往上翻阅时(距底部>60px)不强制吸底, 不打断阅读
  const nearBottom = logPane.scrollHeight - logPane.scrollTop -
    logPane.clientHeight < 60
  const line = document.createElement('div')
  if (tag === 'watch') {
    line.className = 'l-watch'
    line.textContent = '  │ ' + msg
  } else {
    const ts = new Date().toTimeString().slice(0, 8)
    line.className = 'l-' + tag
    line.textContent = ts + '  ' + msg
  }
  logPane.appendChild(line)
  while (logPane.childElementCount > 800) logPane.removeChild(logPane.firstChild)
  if (nearBottom) logPane.scrollTop = logPane.scrollHeight
}

function pickTag(level, msg) {
  if (/ERROR|CRITICAL|失败|offline|✗/.test(msg) || level === 'ERROR') return 'err'
  if (/WARNING|WARN|恢复|重试|停止/.test(msg) || level === 'WARNING') return 'warn'
  if (/PASS|成功|完成|✓|就绪/.test(msg)) return 'ok'
  return 'muted'
}

function capLog() {
  try {
    if (parseInt(logPane.childElementCount, 10) > 800) {
      while (logPane.childElementCount > 500) logPane.removeChild(logPane.firstChild)
    }
  } catch (_e) { /* noop */ }
}

setInterval(capLog, 5000)

// ---- 入口 ----

init()

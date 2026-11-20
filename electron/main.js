// 游戏轮换控制台 - Electron 主进程
// 职责: 窗口 + 引擎子进程管理 (runtime/python + rotate.py) + IPC
// 与引擎的通信沿用文件契约: stdout 日志流 / status.json / stop.flag / config.json
'use strict'

const { app, BrowserWindow, ipcMain, dialog, shell, Tray, Menu, Notification, screen } = require('electron')
const path = require('path')
const fs = require('fs')
const { spawn } = require('child_process')
const readline = require('readline')

// ---- 路径解析: 打包后 exe 与 rotate.py/config.json/logs 同级 (轮换启动器根目录) ----
const isPackaged = app.isPackaged
const ROOT = isPackaged ? path.dirname(process.execPath) : path.join(__dirname, '..')
const PYTHON = path.join(ROOT, 'runtime', 'python', 'python.exe')
const ENGINE = path.join(ROOT, 'rotate.py')
const CONFIG = path.join(ROOT, 'config.json')
const STATUS_FILE = path.join(ROOT, 'status.json')
const STOP_FLAG = path.join(ROOT, 'stop.flag')
const LOG_DIR = path.join(ROOT, 'logs')

const RUNNING_PHASES = new Set(['emulator', 'game', 'recovering', 'shutting_down'])
// 引擎心跳最长间隔 (<60s) 留足余量; 单次阻塞上限 90s (UIA/探针子进程)
const STALE_SEC = 150

let win = null
let tray = null
let engine = null // 引擎子进程 (rotate.py run)
let tool = null // 工具子进程 (check/detect-packages)
let watchFile = '' // 当前游戏脚本日志 (实时输出)
let watchOffset = 0
let emuTimer = null
let statusTimer = null
let watchTimer = null
let isQuitting = false
let hideHintShown = false
let scheduleTimer = null
const ICON = path.join(__dirname, 'renderer', 'assets', 'icon.png')

function readJson(p) {
  try {
    return JSON.parse(fs.readFileSync(p, 'utf-8'))
  } catch (_e) {
    return null
  }
}

// 原子写: 先写临时文件再改名, 避免读方(渲染层/引擎)读到半截 JSON
function writeJsonAtomic(p, obj, indent) {
  const tmp = p + '.tmp'
  try {
    fs.writeFileSync(tmp, JSON.stringify(obj, null, indent === undefined ? 2 : indent), 'utf-8')
    fs.renameSync(tmp, p)
    return true
  } catch (_e) {
    try { fs.unlinkSync(tmp) } catch (_e2) {}
    return false
  }
}

function send(ch, payload) {
  if (win && !win.isDestroyed()) win.webContents.send(ch, payload)
}

function nowStr() {
  const d = new Date()
  const p = (n) => String(n).padStart(2, '0')
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
    ' ' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds())
}

function staleSec(st) {
  const ts = Date.parse((st.updated_at || '').replace(' ', 'T'))
  return isNaN(ts) ? 0 : (Date.now() - ts) / 1000
}

// 引擎死亡但状态停在运行态时自愈 (强杀/崩溃/外部引擎异常退出)
// staleOnly=true 时只处理心跳过期的状态, 避免把仍在外部运行(命令行/另一实例)的引擎误判为已死
function healStatus(staleOnly) {
  const st = readJson(STATUS_FILE)
  if (!st || !RUNNING_PHASES.has(st.phase)) return
  if (staleOnly && staleSec(st) <= STALE_SEC) return
  st.phase = 'stopped'
  st.current = ''
  st.attempt = 0
  st.watch_file = ''
  for (const g of st.games || []) {
    if (g.status === 'pending' || g.status === 'running') g.status = 'skipped'
  }
  st.updated_at = nowStr()
  writeJsonAtomic(STATUS_FILE, st, 1)
}

function busy() {
  if (engine) return true
  const st = readJson(STATUS_FILE)
  if (!st || !RUNNING_PHASES.has(st.phase)) return false
  // 心跳过期 = 引擎已死(强杀/崩溃/断电): 不能让残留状态永久挡住开始与定时
  return staleSec(st) <= STALE_SEC
}

// ---- 引擎管理 ----

function startEngine(trigger) {
  if (engine) return { ok: false, msg: '轮换已在运行中' }
  if (busy()) {
    // 外部引擎(命令行/上次会话遗留)在跑: 允许「接管」——发停止信号后再启动
    return { ok: false, msg: '已有外部轮换引擎在运行。请先点「停止」终止它, 再开始', external: true }
  }
  if (!fs.existsSync(PYTHON)) return { ok: false, msg: '缺少运行时: ' + PYTHON }
  try { fs.unlinkSync(STOP_FLAG) } catch (_e) {}
  engine = spawn(PYTHON, [ENGINE, 'run'], {
    cwd: ROOT,
    env: Object.assign({}, process.env, {
      PYTHONIOENCODING: 'utf-8',
      PYTHONUNBUFFERED: '1',
      ROTATE_TRIGGER: trigger || 'manual',
    }),
    windowsHide: true,
  })
  readline.createInterface({ input: engine.stdout }).on('line', (l) => send('log', l))
  readline.createInterface({ input: engine.stderr }).on('line', (l) => send('log', '[stderr] ' + l))
  engine.on('exit', (code) => {
    engine = null
    healStatus() // 引擎若被强杀, 把状态从运行态拉回停止态
    try { // 完成系统通知
      const n = new Notification({
        title: '轮换已结束',
        body: code === 0 ? '全部游戏成功 ✓' : '存在失败或跳过的游戏, 详情见日志',
        icon: ICON,
        silent: false,
      })
      n.on('click', () => { win.show(); win.focus() })
      n.show()
    } catch (_e) {}
    send('engine-exit', code)
  })
  send('log', '[GUI] 引擎已启动 (rotate.py run)')
  return { ok: true }
}

function stopEngine() {
  fs.writeFileSync(STOP_FLAG, '')
  send('log', '[GUI] 已发送停止请求, 数秒内终止当前脚本并跳过剩余游戏 (模拟器保持运行)')
  if (engine) {
    setTimeout(() => { // 引擎未在 6s 内自行退出则强杀进程树
      if (engine) {
        spawn('taskkill', ['/PID', String(engine.pid), '/T', '/F'], { windowsHide: true })
      }
    }, 6000).unref()
  } else {
    // 外部引擎: 按 engine.lock 里的 PID 强杀 (stop.flag 通常已足够, 这是兜底)
    setTimeout(() => {
      if (!busy()) return
      try {
        const lock = fs.readFileSync(path.join(ROOT, 'engine.lock'), 'utf-8')
        const pid = lock.trim().split(':')[0]
        if (pid) {
          spawn('taskkill', ['/PID', pid, '/T', '/F'], { windowsHide: true })
          send('log', '[GUI] 外部引擎未响应停止信号, 已强制终止 (PID ' + pid + ')')
        }
      } catch (_e) {}
    }, 12000).unref()
  }
  return { ok: true }
}

// ---- 工具 (check / detect-packages) ----

function runTool(name, extraArgs) {
  if (tool) return { ok: false, msg: '工具运行中' }
  if (busy()) return { ok: false, msg: '轮换运行中, 暂不执行工具' }
  if (!fs.existsSync(PYTHON)) return { ok: false, msg: '缺少运行时: ' + PYTHON }
  tool = spawn(PYTHON, [ENGINE, name].concat(extraArgs || []), {
    cwd: ROOT,
    env: Object.assign({}, process.env, { PYTHONIOENCODING: 'utf-8' }),
    windowsHide: true,
  })
  readline.createInterface({ input: tool.stdout }).on('line', (l) => send('log', l))
  readline.createInterface({ input: tool.stderr }).on('line', (l) => send('log', '[stderr] ' + l))
  tool.on('exit', (code) => {
    tool = null
    send('log', '[GUI] ' + name + ' 结束 (退出码 ' + code + ')')
    send('tool-done', name) // 渲染层借此重新加载配置
  })
  return { ok: true }
}

// ---- 定时调度: 到点自动开始轮换 (错过后当日补跑一次) ----

function checkSchedule() {
  const cfg = readJson(CONFIG)
  const s = cfg && cfg.schedule
  if (!s || !s.enabled) return
  if (busy()) return
  const today = nowStr().slice(0, 10)
  if ((s.last_run_date || '') === today) return
  const t = padHM(s.time)
  if (nowStr().slice(11, 16) < t) return
  // 先真正启动、成功后才记账 —— 启动失败(残留状态/缺运行时)不能把当天吞掉
  const r = startEngine('schedule')
  if (!r.ok) {
    send('log', '[调度] 到点启动失败, 不记为今日已跑: ' + (r.msg || ''))
    return
  }
  try {
    const c2 = readJson(CONFIG) || {}
    c2.schedule = Object.assign({}, c2.schedule, { last_run_date: today })
    writeJsonAtomic(CONFIG, c2)
  } catch (_e) {}
  send('log', '[调度] 到达每日定时 ' + t + ', 自动开始轮换')
}

// 把 "9:30" 归一化为 "09:30"; 非法值退回 03:30 (字符串比较才有意义)
function padHM(t) {
  const m = /^(\d{1,2}):(\d{2})$/.exec(String(t || '').trim())
  if (!m) return '03:30'
  const h = Number(m[1])
  const mi = Number(m[2])
  if (h > 23 || mi > 59) return '03:30'
  return String(h).padStart(2, '0') + ':' + m[2]
}

// ---- 轮询: 状态 / 模拟器 / 当前游戏日志 ----

function startPolling() {
  statusTimer = setInterval(() => {
    const st = readJson(STATUS_FILE)
    if (st) {
      // 无本进程引擎 + 状态心跳超时 (STALE_SEC) = 引擎已死, 自愈并推送修正后的状态
      // (test-game 等工具进程也在写状态: 工具在跑时不自愈, 工具退出后下次轮询会处理)
      if (RUNNING_PHASES.has(st.phase) && !engine && !tool && staleSec(st) > STALE_SEC) {
        healStatus(true)
        send('status', readJson(STATUS_FILE) || st)
        return
      }
      send('status', st)
    }
  }, 1000)

  emuTimer = setInterval(() => {
    const cfg = readJson(CONFIG)
    if (!cfg) return
    const ec = cfg.emulator || {}
    if (!ec.mumu_manager) return
    const p = spawn(ec.mumu_manager, ['info', '-v', String(ec.vm_index || 0)], {
      windowsHide: true,
    })
    let out = ''
    p.stdout.on('data', (d) => (out += d.toString('utf-8')))
    p.on('error', () => {})
    p.on('close', () => {
      let state = '未运行'
      let color = '#d93742'
      if (out.includes('"is_android_started": true')) {
        state = '运行中 · Android 就绪'
        color = '#1f9d55'
      } else if (out.includes('"is_process_started": true')) {
        state = '启动中'
        color = '#c07f00'
      }
      send('emu', { state, color })
    })
  }, 5000)

  watchTimer = setInterval(() => {
    const st = readJson(STATUS_FILE)
    const wf = (st && st.watch_file) || ''
    if (wf !== watchFile) {
      watchFile = wf
      watchOffset = 0
      if (wf && fs.existsSync(wf)) {
        const size = fs.statSync(wf).size
        watchOffset = Math.max(0, size - 40000)
        send('watch', '── 输出来源: ' + path.basename(wf) + ' ──')
      }
    }
    if (watchFile && fs.existsSync(watchFile)) {
      const size = fs.statSync(watchFile).size
      if (size < watchOffset) watchOffset = 0
      if (size > watchOffset) {
        const fd = fs.openSync(watchFile, 'r')
        const buf = Buffer.alloc(size - watchOffset)
        fs.readSync(fd, buf, 0, buf.length, watchOffset)
        fs.closeSync(fd)
        watchOffset = size
        for (const line of buf.toString('utf-8').split('\n')) {
          if (line) send('watch', line.trimEnd())
        }
      }
    }
  }, 1000)
}

// ---- 窗口 ----

function createTray() {
  try {
    tray = new Tray(ICON)
    const menu = Menu.buildFromTemplate([
      { label: '显示窗口', click: () => { win.show(); win.focus() } },
      { label: '停止轮换', click: () => stopEngine() },
      { type: 'separator' },
      { label: '退出', click: () => quitApp() },
    ])
    tray.setContextMenu(menu)
    tray.setToolTip('游戏轮换控制台')
    tray.on('double-click', () => { win.show(); win.focus() })
  } catch (_e) {
    tray = null
  }
}

function quitApp() {
  if (!engine) return closeForQuit()
  dialog.showMessageBox({
    type: 'warning',
    buttons: ['取消', '退出并停止轮换'],
    defaultId: 1,
    cancelId: 0,
    message: '轮换正在进行中',
    detail: '退出将停止引擎并终止当前脚本进程, 确定吗?',
  }).then(({ response }) => {
    if (response === 1) closeForQuit()
  }).catch(() => {})
}

function closeForQuit() {
  const finish = () => {
    isQuitting = true
    try { if (win) win.destroy() } catch (_e) {}
    app.quit()
  }
  if (!engine) return finish()
  try { fs.writeFileSync(STOP_FLAG, '') } catch (_e) {}
  setTimeout(() => { // 引擎 2.5s 内未自行退出则强杀进程树
    if (engine) {
      try {
        spawn('taskkill', ['/PID', String(engine.pid), '/T', '/F'],
          { windowsHide: true })
      } catch (_e) {}
    }
    finish()
  }, 2500)
}

function saveClosePref(v) {
  const cfg = readJson(CONFIG) || {}
  cfg.window = Object.assign({}, cfg.window, { close: v })
  writeJsonAtomic(CONFIG, cfg)
}

function hideToTray() {
  win.hide()
  if (!hideHintShown) {
    hideHintShown = true
    try {
      const n = new Notification({
        title: '已最小化到托盘',
        body: '轮换仍在后台运行, 双击托盘图标回到窗口',
        icon: ICON,
        silent: true,
      })
      n.show()
    } catch (_e) {}
  }
}

function debounce(fn, ms) {
  let t = null
  return function () {
    clearTimeout(t)
    t = setTimeout(fn, ms)
  }
}

// 窗口尺寸: 有记住的尺寸就用它 (并夹到当前屏幕工作区内), 否则按屏幕比例给一个合适的
function computeBounds() {
  const cfg = readJson(CONFIG) || {}
  const saved = cfg.window && cfg.window.bounds
  const savedRect = saved && saved.width > 200 && saved.height > 150
    ? { x: saved.x || 0, y: saved.y || 0, width: saved.width, height: saved.height }
    : screen.getPrimaryDisplay().workArea
  const wa = screen.getDisplayMatching(savedRect).workArea
  if (saved && saved.width > 200 && saved.height > 150) {
    const width = Math.min(Math.max(saved.width, 760), wa.width)
    const height = Math.min(Math.max(saved.height, 560), wa.height)
    const x = Math.min(Math.max(saved.x === undefined ? wa.x : saved.x, wa.x),
      wa.x + wa.width - width)
    const y = Math.min(Math.max(saved.y === undefined ? wa.y : saved.y, wa.y),
      wa.y + wa.height - height)
    return { x, y, width, height }
  }
  // 默认: 稍宽、别太高 (940x960 在小屏上会顶到任务栏)
  const width = Math.min(1120, Math.max(900, Math.round(wa.width * 0.55)))
  const height = Math.min(860, Math.max(700, Math.round(wa.height * 0.78)))
  return { width, height, center: true }
}

function createWindow() {
  const bounds = computeBounds()
  win = new BrowserWindow(Object.assign({
    minWidth: 760,
    minHeight: 560,
    backgroundColor: '#f4f6fa',
    autoHideMenuBar: true,
    title: '游戏轮换控制台',
    icon: ICON,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  }, bounds))

  // 记住窗口位置/大小 (缩放或移动停下 500ms 后落盘)
  const saveBounds = debounce(() => {
    if (!win || win.isDestroyed() || win.isMinimized() ||
        win.isMaximized() || win.isFullScreen()) return
    try {
      const cfg = readJson(CONFIG) || {}
      cfg.window = Object.assign({}, cfg.window, { bounds: win.getBounds() })
      writeJsonAtomic(CONFIG, cfg)
    } catch (_e) {}
  }, 500)
  win.on('resize', saveBounds)
  win.on('move', saveBounds)
  win.loadFile(path.join(__dirname, 'renderer', 'index.html'))
  // 渲染层错误捕获 → 落盘 (远程诊断)
  win.webContents.on('console-message', (_e, level, message, line, sourceId) => {
    try {
      fs.appendFileSync(
        path.join(LOG_DIR, 'renderer.log'),
        new Date().toISOString() + ' L' + line + ' [' + level + '] ' +
          message + ' (' + path.basename(String(sourceId)) + ')\n',
        'utf-8')
    } catch (_e) {}
  })
  // 关窗: 按记住的偏好执行; 未记住时弹三选一 (托盘/退出/取消)
  win.on('close', (e) => {
    if (isQuitting) return
    const pref = (readJson(CONFIG) || {}).window?.close || 'ask'
    if (pref === 'tray') {
      e.preventDefault()
      hideToTray()
      return
    }
    e.preventDefault()
    dialog.showMessageBox(win, {
      type: 'question',
      buttons: ['最小化到托盘', '退出应用', '取消'],
      defaultId: 0,
      cancelId: 2,
      checkboxLabel: '记住我的选择',
      checkboxChecked: false,
      message: '关闭窗口',
      detail: engine
        ? '轮换仍在后台继续运行；「退出应用」会先停止轮换再退出。'
        : '最小化到托盘后，双击托盘图标回到窗口。',
    }).then(({ response, checkboxChecked }) => {
      if (response === 0) {
        hideToTray()
        if (checkboxChecked) saveClosePref('tray')
      } else if (response === 1) {
        if (checkboxChecked) saveClosePref('exit')
        closeForQuit()
      }
    }).catch(() => {})
  })
}

const gotLock = app.requestSingleInstanceLock()
if (!gotLock) {
  app.quit()
} else {
  app.on('second-instance', () => {
    if (win) {
      if (win.isMinimized()) win.restore()
      win.focus()
    }
  })

  app.whenReady().then(() => {
    app.setAppUserModelId('com.sonjayce.rotation-console') // Windows 通知必需
    createWindow()
    if (process.argv.includes('--hidden')) win.hide() // 开机自启: 静默缩托盘
    createTray()
    startPolling()
    healStatus(true)   // 只自愈心跳过期的残留运行态; 不碰外部正在跑的引擎
    checkSchedule() // 启动即补检一次 (错过定时当日补跑)
    scheduleTimer = setInterval(checkSchedule, 30000)
  })

  app.on('window-all-closed', () => app.quit())
}

// ---- IPC ----

ipcMain.handle('init', () => ({
  root: ROOT,
  config: readJson(CONFIG),
  status: readJson(STATUS_FILE),
  hasRuntime: fs.existsSync(PYTHON),
}))

ipcMain.handle('start', () => startEngine())
ipcMain.handle('stop', () => stopEngine())
ipcMain.handle('tool', (_e, name) => runTool(name))
ipcMain.handle('open-logs', () => {
  try { fs.accessSync(LOG_DIR) } catch (_e) { return }
  shell.openPath(LOG_DIR)
})

ipcMain.handle('get-history', () => {
  try {
    const raw = fs.readFileSync(path.join(ROOT, 'history.jsonl'), 'utf-8')
    return raw.trim().split('\n').slice(-50).reverse()
      .map((l) => { try { return JSON.parse(l) } catch (_e) { return null } })
      .filter(Boolean)
  } catch (_e) {
    return []
  }
})

ipcMain.handle('emu-action', (_e, action) => {
  if (busy()) return { ok: false, msg: '轮换运行中, 禁止操作模拟器' }
  const cfg = readJson(CONFIG)
  if (!cfg) return { ok: false, msg: '配置读取失败' }
  const ec = cfg.emulator || {}
  if (!ec.mumu_manager) return { ok: false, msg: '未配置 mumu_manager' }
  const args = action === 'start'
    ? ['control', '-v', String(ec.vm_index || 0), 'launch']
    : ['control', '-v', String(ec.vm_index || 0), 'shutdown']
  const p = spawn(ec.mumu_manager, args, { windowsHide: true })
  p.on('error', (err) => send('log', '[GUI] 模拟器指令失败: ' + err.message))
  p.on('close', (code) => {
    send('log', '[GUI] 模拟器' + (action === 'start' ? '启动' : '关闭') +
      '指令已发送 (退出码 ' + code + ')')
  })
  return { ok: true }
})

ipcMain.handle('set-enabled', (_e, gid, val) => {
  const cfg = readJson(CONFIG)
  if (cfg && cfg.games && cfg.games[gid]) {
    cfg.games[gid].enabled = !!val
    if (!writeJsonAtomic(CONFIG, cfg)) return { ok: false, msg: '配置写入失败' }
    return { ok: true }
  }
  return { ok: false }
})

// 拖动调整轮换顺序 (只接受现有队列的重新排列)
ipcMain.handle('set-order', (_e, order) => {
  if (busy()) return { ok: false, msg: '轮换运行中, 不能调整顺序' }
  const cfg = readJson(CONFIG)
  if (!cfg || !cfg.rotation || !Array.isArray(order)) {
    return { ok: false, msg: '参数错误' }
  }
  const cur = cfg.rotation.order || []
  const norm = (a) => [...a].map(String).sort().join('\u0000')
  if (order.length !== cur.length || norm(order) !== norm(cur)) {
    return { ok: false, msg: '顺序与当前队列不一致' }
  }
  cfg.rotation = Object.assign({}, cfg.rotation, { order: order.map(String) })
  if (!writeJsonAtomic(CONFIG, cfg)) return { ok: false, msg: '配置写入失败' }
  return { ok: true }
})

ipcMain.handle('save-settings', (_e, payload) => {
  const cfg = readJson(CONFIG)
  if (!cfg) return { ok: false, msg: '配置读取失败' }
  if (payload.schedule) {
    cfg.schedule = Object.assign({}, cfg.schedule, payload.schedule)
    if (payload.justEnabled) cfg.schedule.last_run_date = nowStr().slice(0, 10)
  }
  if (payload.notify) {
    cfg.notify = Object.assign({}, cfg.notify, payload.notify)
  }
  if (payload.rotation_mute !== undefined) {
    cfg.rotation = Object.assign({}, cfg.rotation,
      { mute_emulator: !!payload.rotation_mute })
  }
  if (payload.rotation_parallel !== undefined) {
    cfg.rotation = Object.assign({}, cfg.rotation,
      { parallel: !!payload.rotation_parallel })
  }
  if (!writeJsonAtomic(CONFIG, cfg)) return { ok: false, msg: '配置写入失败' }
  // 开机自启 (仅打包版; --hidden 静默启动)
  if (app.isPackaged) {
    try {
      app.setLoginItemSettings({
        openAtLogin: !!(cfg.schedule && cfg.schedule.autostart),
        args: ['--hidden'],
        path: process.execPath,
      })
    } catch (_e) {}
  }
  return { ok: true }
})

// ---- 游戏/脚本 傻瓜式添加 (向导) ----
// 校验交给引擎 rotate.py 的 validate_game 逻辑等价物, 这里仅做最少的成员同步:
// add-game 写 games + 追加 order; update-game 覆盖 games 不动 order; remove-game 双向移除。

function syncOrder(cfg, gid) {
  if (!cfg.rotation) cfg.rotation = {}
  const order = Array.isArray(cfg.rotation.order) ? cfg.rotation.order : []
  if (!order.includes(gid)) {
    cfg.rotation = Object.assign({}, cfg.rotation, { order: order.concat([gid]) })
  }
}

function sanitizeGame(spec) {
  if (!spec || typeof spec !== 'object' || Array.isArray(spec)) return null
  const out = {}
  for (const k of Object.keys(spec)) {
    if (k === 'id' || k === 'gid') continue
    const v = spec[k]
    // 剥离非 JSON 标量/数组/对象 (安全桥传来的都是可序列化的, 这里兜底)
    if (v === undefined || typeof v === 'function' || typeof v === 'symbol') continue
    out[k] = v
  }
  return out
}

// 与 rotate.py 的 validate_game 对齐的轻量校验: 必填字段 + 文件/目录存在性
function gameKind(g) {
  if (!g) return 'marker'
  if (g.type === 'mxu') return 'mxu'
  if (g.type === 'alas') return g.mode === 'ui' ? 'alas-ui' : 'alas-headless'
  return 'marker'
}

const GAME_REQUIRED = {
  marker: { fields: ['exe', 'log_file'], files: ['exe', 'log_file'] },
  'alas-ui': { fields: ['ui_exe'], files: ['ui_exe'] },
  'alas-headless': { fields: ['dir', 'python', 'entry'], files: ['python', 'entry'], dirs: ['dir'] },
  mxu: { fields: ['exe'], files: ['exe'] },
}

// 与 rotate.py 的 defaults_for_type 保持一致: 新条目补齐默认值, config.json 才看得懂
const GAME_DEFAULTS = {
  marker: {
    type: 'marker', args: '', package: '', package_keywords: [], timeout_min: 60,
    success_grace_sec: 20, success_markers: [], fail_markers: [], launch_game: true,
    launch_probe: false, launch_taps: [], needs_emulator: true, recovery_mode: 'full',
  },
  'alas-ui': {
    type: 'alas', mode: 'ui', ui_port: 22267, instance: 'alas', keep_ui: true,
    ui_launch_timeout_sec: 120, stall_sec: 600, log_dir: '', dir: '', package: '',
    package_keywords: [], timeout_min: 120, idle_sec: 180, fail_markers: [],
    non_retryable_markers: [], launch_game: true, launch_taps: [], launch_probe: true,
    needs_emulator: true, recovery_mode: 'full',
  },
  'alas-headless': {
    type: 'alas', timeout_min: 120, idle_sec: 180, fail_markers: [],
    non_retryable_markers: [], package: '', package_keywords: [], launch_game: true,
    launch_taps: [], launch_probe: true, needs_emulator: true, recovery_mode: 'full',
  },
  mxu: {
    type: 'mxu', args: '', dir: '', note: '', launch_game: false, ui_wait_sec: 12,
    start_button: '开始任务', env: {}, log_dir: '', success_markers: [],
    fail_markers: [], started_markers: ['开始执行任务'], timeout_min: 90,
    task_start_wait_sec: 120, needs_emulator: false, recovery_mode: 'light',
    game_task: '', game_process: '', game_wait_sec: 180, game_settle_sec: 20,
  },
}

function withDefaults(game) {
  const base = GAME_DEFAULTS[gameKind(game)]
  return Object.assign({ name: '', enabled: true }, base, game)
}

// 与 rotate.py 的 fill_derived 对齐: 能从 exe/ui_exe 推导的路径不用用户填
function fillDerived(g) {
  const out = Object.assign({}, g)
  if (out.type === 'alas') {
    if (!out.dir) {
      const base = out.ui_exe || out.entry || out.python || ''
      if (base) {
        let d = path.dirname(path.resolve(base))
        if (out.mode !== 'ui' && path.basename(d).toLowerCase() === 'toolkit') {
          d = path.dirname(d)
        }
        out.dir = d
      }
    }
  } else if (out.type === 'mxu') {
    if (!out.dir && out.exe) out.dir = path.dirname(path.resolve(out.exe))
    if (!out.log_dir && out.dir) {
      const d = path.join(out.dir, 'debug')
      try { if (fs.statSync(d).isDirectory()) out.log_dir = d } catch (_e) {}
    }
  }
  return out
}

function validateGameSpec(gid, game) {
  const errs = []
  if (!game || typeof game !== 'object') return [gid + ': 条目无效']
  if (!String(game.name || '').trim()) errs.push(gid + ': 缺少名称')
  if (!['marker', 'alas', 'mxu'].includes(game.type)) {
    errs.push(gid + ': type 非法 (' + String(game.type) + ')')
    return errs
  }
  const spec = GAME_REQUIRED[gameKind(game)]
  for (const f of spec.fields) {
    if (game[f] === undefined || game[f] === null || game[f] === '') {
      errs.push(gid + ': 缺少 ' + f)
    }
  }
  for (const f of spec.files || []) {
    const v = game[f]
    if (!v) continue
    if (f === 'log_file') {
      // 日志文件常在首次运行后才出现 (可能带 {today} 占位): 只查父目录
      const dir = path.dirname(String(v).replace('{today}', '20000101'))
      if (!fs.existsSync(dir)) errs.push(gid + ': 日志目录不存在 ' + dir)
      continue
    }
    if (!fs.existsSync(v)) errs.push(gid + ': 文件不存在 ' + f + '=' + v)
  }
  for (const f of spec.dirs || []) {
    if (game[f] && !fs.existsSync(game[f])) {
      errs.push(gid + ': 目录不存在 ' + f + '=' + game[f])
    }
  }
  const tm = Number(game.timeout_min)
  if (!(tm > 0)) errs.push(gid + ': 硬超时必须是大于 0 的分钟数')
  return errs
}

ipcMain.handle('add-game', (_e, gid, spec) => {
  if (busy()) return { ok: false, msg: '轮换运行中, 不能添加游戏' }
  const cfg = readJson(CONFIG)
  if (!cfg) return { ok: false, msg: '配置读取失败' }
  gid = String(gid || '').trim()
  if (!gid) return { ok: false, msg: '游戏 id 不能为空' }
  if (/[^\w.-]/.test(gid)) return { ok: false, msg: '游戏 id 只能含字母/数字/_.-' }
  if (cfg.games && cfg.games[gid]) return { ok: false, msg: '游戏 id 已存在: ' + gid }
  const game = sanitizeGame(spec)
  if (!game) return { ok: false, msg: '游戏定义无效' }
  const full = fillDerived(withDefaults(game))
  const errs = validateGameSpec(gid, full)
  if (errs.length) return { ok: false, msg: errs[0], errs }
  if (!cfg.games) cfg.games = {}
  cfg.games[gid] = full
  syncOrder(cfg, gid)
  if (!writeJsonAtomic(CONFIG, cfg)) return { ok: false, msg: '配置写入失败' }
  return { ok: true }
})

ipcMain.handle('update-game', (_e, gid, spec) => {
  if (busy()) return { ok: false, msg: '轮换运行中, 不能编辑游戏' }
  const cfg = readJson(CONFIG)
  if (!cfg) return { ok: false, msg: '配置读取失败' }
  gid = String(gid || '').trim()
  if (!cfg.games || !cfg.games[gid]) return { ok: false, msg: '游戏不存在: ' + gid }
  const game = sanitizeGame(spec)
  if (!game) return { ok: false, msg: '游戏定义无效' }
  const merged = Object.assign({}, cfg.games[gid], game)
  const full = fillDerived(withDefaults(merged))
  const errs = validateGameSpec(gid, full)
  if (errs.length) return { ok: false, msg: errs[0], errs }
  cfg.games[gid] = full
  if (!writeJsonAtomic(CONFIG, cfg)) return { ok: false, msg: '配置写入失败' }
  return { ok: true }
})

ipcMain.handle('remove-game', (_e, gid) => {
  if (busy()) return { ok: false, msg: '轮换运行中, 不能删除游戏' }
  const cfg = readJson(CONFIG)
  if (!cfg) return { ok: false, msg: '配置读取失败' }
  gid = String(gid || '').trim()
  if (!cfg.games || !cfg.games[gid]) return { ok: false, msg: '游戏不存在: ' + gid }
  delete cfg.games[gid]
  if (cfg.rotation && Array.isArray(cfg.rotation.order)) {
    cfg.rotation = Object.assign({}, cfg.rotation,
      { order: cfg.rotation.order.filter((g) => g !== gid) })
  }
  if (!writeJsonAtomic(CONFIG, cfg)) return { ok: false, msg: '配置写入失败' }
  return { ok: true }
})

// 单游戏试跑: 复用 runTool 的日志/完成推送, 但走 test-game 子命令
ipcMain.handle('test-game', (_e, gid) => runTool('test-game', [String(gid || '')]))

// 文件/目录选择器 (向导用)
ipcMain.handle('choose-file', async (_e, filters) => {
  const r = await dialog.showOpenDialog(win, {
    properties: ['openFile'],
    filters: Array.isArray(filters) ? filters : undefined,
  })
  return r.canceled ? null : (r.filePaths && r.filePaths[0]) || null
})

ipcMain.handle('choose-dir', async (_e) => {
  const r = await dialog.showOpenDialog(win, { properties: ['openDirectory'] })
  return r.canceled ? null : (r.filePaths && r.filePaths[0]) || null
})

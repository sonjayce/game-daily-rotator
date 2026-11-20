# Game Daily Rotator · 游戏日常轮换控制台

一个 Windows 桌面控制台：把多款游戏的日常脚本排成队列，每天自动
**启动 MuMu 模拟器 → 逐个运行脚本（自动判定成败、失败自愈重试）→ 汇总推送 → 关闭模拟器**。

- Electron 界面（白色「作战指挥台」风格），脚本引擎为纯标准库 Python，二者经文件契约解耦
- 内置**傻瓜式添加游戏向导**：选模板 → 选 exe → 填一两个标记词 → 保存 → 单测验证 → 进队列
- 支持模拟器游戏（MuMu 12）与 PC 端脚本（如 MaaEnd）混合编排，PC 通道可与模拟器通道**并行**

> 适合：手里有多款挂日常的脚本（MAA 系 / MFAAvalonia 系 / ALAS 等），想每天定时全自动跑完并收到汇总通知。

---

## 特性一览

| 能力 | 说明 |
|---|---|
| 🎯 轮换队列 | 拖拽排序、单游戏启用开关、实时进度（时钟/耗时/第几次尝试） |
| ➕ 傻瓜式添加 | 4 种内置模板 + 文件选择器 + 路径自动推导 + 保存即校验 |
| 🧪 单游戏试跑 | 配置完先只跑这一个验证，模拟器保持运行方便改了再测 |
| 🔁 失败自愈 | 按类别诊断（超时/崩溃/失败标记/模拟器失联/游戏未启动→可重试；需人工→跳过） |
| 🛠 恢复策略 | `full`=重启模拟器整套环境；`light`=只重启游戏 APP（保住更新下载进度） |
| 🕒 定时任务 | 每日定时自动开跑，错过当日补跑一次；开机自启静默缩托盘 |
| 📩 结果推送 | Server酱 / PushPlus / Telegram，跑完推汇总到手机 |
| 🔇 细节体验 | 轮换期自动静音模拟器、失败复跑（second_pass）、维护检测、ADB 三级自愈 |
| 🩺 一键体检 | 20+ 检查项（路径/串口/包名/锁残留），安全项自动修复 |

## 界面

```
┌────────────────────────────────────────────┐
│ 游戏轮换控制台          模拟器: 运行中 ●    │ 顶栏
├────────────────────────────────────────────┤
│ █ 墨黑任务时钟: 大号计时 / 进度流光 / 下次定时 │
├────────────────────────────────────────────┤
│ 01 ● 明日方舟：终末地   进行中  15:09       │ 时间轴队列
│ 02 ● 火影忍者          已完成  33:35       │ (拖拽排序 + 测/改/删)
│ 03 ● 明日方舟          已完成  16:33       │
├────────────────────────────────────────────┤
│ [开始轮换] [停止]      [体检][包名][+添加] │
├────────────────────────────────────────────┤
│ ⏰每日定时 | 📩完成推送 | 🔁并行 | 🔇静音   │ 自动化设置条
├────────────────────────────────────────────┤
│ 运行日志 (引擎 + 当前游戏实时输出)          │
└────────────────────────────────────────────┘
```

---

## 快速开始

### 方式 A：下载打包版（推荐）

1. 从 Release 下载整包（或自行构建，见下）解压到任意目录
2. 双击 `游戏轮换控制台.exe`（首次弹一次 UAC，点「是」）
3. 首次运行会自动生成 `config.json` 骨架 → 界面「➕ 添加游戏」向导加入你的脚本

### 方式 B：从源码构建

```bash
# 1) 界面
cd electron
npm install        # .npmrc 已配置 npmmirror 镜像，可按需删除
npm run dist       # 产物在 electron/dist-out/win-unpacked/
cd ..
# 2) 把 win-unpacked 里的 exe 与 resources\ 复制到仓库根目录（或运行 deploy-artifacts.bat）
# 3) 引擎运行时（打包版需要）
setup-runtime.bat  # 下载嵌入式 Python 3.11 到 runtime\python\
```

> 源码方式运行也可以直接 `npx electron .`（界面）+ 命令行引擎（见下），不打包。

### 打 Release 分发包（维护者）

```bash
python build-release.py
```

把 `win-unpacked/` + `runtime/python/` + 引擎文件 + `config.example.json` + README 组装成
`release/game-daily-rotator-v<版本>-win-x64.zip`（约 110 MB，解压即用，无需再配置），
并生成同名 `.sha256` 校验文件。版本号取自 `electron/package.json`，上传到 GitHub Releases 即可。

### 前置条件

- **MuMu 模拟器 12**（多开实例取 `vm_index`，默认 0）；路径填进 `config.json` 的 `emulator` 段
- 各游戏的日常脚本本体（MAA / MFAAvalonia / ALAS / MaaEnd 等），向导里指向它们的 exe
- PC 端游戏如需管理员权限启动（如终末地），建议先手动创建一个 HIGHEST 计划任务，
  在向导的「计划任务名」里填名字即可静默启动、不弹 UAC

---

## 使用指南

### ➕ 添加游戏（向导）

点操作条 **「➕ 添加游戏」**，三步：

**① 选模板**（决定轮换器怎么判定“跑完了”）：

| 模板 | 适合 | 判定方式 |
|---|---|---|
| 通用脚本 | MFAAvalonia / MAA 这类自己跑完的 GUI 脚本 | 监控日志出现「成功标记」 |
| ALAS 界面 | AzurLaneAutoScript 的 `Alas.exe`（可看画面/随时干预） | 调度器空闲 3 分钟 |
| ALAS 无头 | 不开界面，直接用 ALAS 自带 python 跑 `alas.py` | 同上 |
| MaaEnd / PC 端 | MaaEnd 这类 Tauri 桌面程序 | 自动点「开始任务」后监控日志 |

**② 填表单**：exe / 日志路径用「选择」按钮挑；程序目录、debug 日志目录**留空自动从 exe
位置推导**；成功/失败标记一行一个（例：`任务已全部完成`）；PC 端模板会自动关闭
「拉起游戏」与「需要模拟器」。

**③ 保存 → 试跑**：队列卡片 hover 出现 **测 / 改 / 删**：

- **测** = 单游戏试跑（校验 + 按需起模拟器 + 跑一遍 + 不关模拟器），日志实时滚进底部日志区
- **改** = 重新打开向导，未展示的字段（如 `launch_taps`、`env`）自动保留
- **删** = 二次确认后从队列移除（自动备份 `config.json` 到 `config.json.bak-*`）

轮换运行中以上操作与排序、开关一律锁定。

### 🔁 并行 vs 串行

设置条「🔁 终末地并行」开关（`rotation.parallel`）：

- **开**（默认）：`needs_emulator: false` 的 PC 端游戏与模拟器游戏**同时运行**，总时长取较长者
- **关**：严格按队列顺序串行

### ⏰ 定时 / 📩 推送 / 🔇 静音

- **每日自动轮换**：设定时间到点自动开跑；错过（应用没开着）当日补跑一次；
  「自启」= 开机静默缩托盘。引擎单实例锁保证定时与手动互不打架
- **完成推送**：Server酱（SendKey）/ PushPlus / Telegram（token + chat_id），填完点「测试」
- **轮换时静音**：开跑自动静音模拟器（MuMu 自身声音开关），结束恢复原状态

### 点 ✕ 关窗

弹三选一：**最小化到托盘**（轮换继续）/ **退出应用**（先优雅停引擎再退）/ 取消；
可勾选「记住我的选择」。托盘双击回窗、右键可停止/退出。

---

## 命令行（可选，适合 Windows 计划任务）

```text
start_rotation.bat                  # 完整跑一轮 (约 1~2 小时)
start_rotation.bat doctor           # 体检 (只读)
start_rotation.bat doctor-fix      # 体检并自动修复安全项 (ALAS 串口/截图方式等)
start_rotation.bat detect-packages  # 起模拟器按关键词识别游戏包名并写回 config
python rotate.py add-game game.json   # JSON 添加游戏 (与向导同一套校验/默认值)
python rotate.py remove-game <id>
python rotate.py test-game <id>       # 单游戏试跑
start_rotation.bat setup            # 一次性修复 ALAS 连接配置 (自动备份)
```

命令行与界面共用 `rotate.py` 引擎与 `config.json`：命令行跑轮换时界面同样显示状态，
停止按钮也有效（stop.flag）。

## 添加/删除游戏的 JSON 格式（CLI）

```jsonc
{
  "id": "naruto",              // 必填, 字母/数字/_-. ；其余字段同 config.json 的 games 条目
  "name": "火影忍者",           // 必填
  "type": "marker",            // marker | alas(配 mode: ui/headless) | mxu
  "exe": "D:/MFAAvalonia/MFAAvalonia.exe",
  "log_file": "D:/MFAAvalonia/logs/log-{today}.log",  // {today} = YYYYMMDD
  "success_markers": ["任务已全部完成"],
  "timeout_min": 60
}
```

未填的可选字段由引擎按模板默认值补齐（超时 60/90/120 分钟、idle 180s、恢复策略等）。

---

## 配置参考（config.json）

| 键 | 说明 |
|---|---|
| `emulator.mumu_manager / adb` | MuMu 的 MuMuManager.exe 与 adb.exe 路径 |
| `emulator.adb_serial / vm_index` | ADB 串口（默认 `127.0.0.1:16384`）与实例号 |
| `rotation.order` | 游戏顺序（界面拖拽自动维护） |
| `games.<id>.enabled` | 临时禁用某游戏（界面开关更方便） |
| `games.<id>.timeout_min` | 硬超时（分钟），到点强杀进程树 |
| `games.<id>.recovery_mode` | `full` 重启模拟器整套 / `light` 只重启游戏 APP |
| `games.<id>.retry_delay_sec` | 重试冷却（更新下载场景给足时间，如方舟 600s） |
| `games.<id>.launch_taps` | `[{at:秒,x,y}]` 启动引导点击（碧蓝冷启动跳过标题页用） |
| `games.<id>.launch_probe` | 用 cv2 模板匹配等标题页（需 `probe_title.py` + `title_template.png`） |
| `games.<id>.game_task` | PC 游戏：用 HIGHEST 计划任务静默启动（绕 UAC） |
| `rotation.close_emulator_after` | 跑完是否关模拟器 |
| `rotation.second_pass` | 全队列跑完后对失败游戏自动复跑一遍 |

## 错误诊断与重试

| 类别 | 处理 |
|---|---|
| `TIMEOUT` / `PROCESS_EXITED` / `FAILED_MARKER` / `EMULATOR_NOT_READY` / `GAME_NOT_LAUNCHED` | 可重试：关游戏 → （`full` 再关模拟器）→ 冷却 → 重跑，默认最多 2 次 |
| `NON_RETRYABLE`（如 ALAS `Request human takeover`） | 不重试，标记 SKIPPED 换下一个 |
| `MAINTENANCE`（日志出现维护提示） | 直接跳过该游戏并推送说明，不浪费重试 |
| 手动停止（ALAS 界面点停止 / 停止按钮） | 记录状态，不触发恢复流程 |

更新/维护场景：轻量恢复保住下载进度 + 冷却期后 second_pass 复跑，通常能自动过关。

## 架构

```
┌─ Electron 主进程 (main.js) ──────────────┐     ┌─ Python 引擎 (rotate.py) ─┐
│ 窗口/托盘/定时调度/模拟器灯/推送触发      │     │ 轮换编排/恢复/诊断/历史/推送 │
│     │ spawn                              │     │   EmulatorCtl (MuMuManager+ADB)
│     ▼  stdout 日志流 ────────────────────►│日志  │   ├ marker/alas/mxu Runner
│ status.json ◄── 心跳+状态快照 (1s 轮询)   │     │   ├ preflight/doctor 校验  │
│ stop.flag  ◄── 停止信号                   │     │   └ engine.lock 单实例     │
└──────────────────────────────────────────┘     └────────────────────────────┘
          渲染层 (app.js) ◄── ipcMain 推送 log/status/emu/watch
```

文件契约（`status.json` / `stop.flag` / `engine.lock` / stdout）让界面、命令行、引擎
互相独立：任何一个死了都能被另外两方检测并自愈。

## 常见问题

- **MAA 启动了但没跑任务**：MAA 设置 → 启动设置 → 勾选「启动后直接运行」(RunDirectly)。
  MAA 自动更新后的首次启动会跳过自动运行（官方行为），轮换器重试一次即恢复
- **ALAS 起不来报 Game page unknown**：新版 MuMu 下 `nemu_ipc` 截图会黑帧，
  「体检修复」会自动改为 `DroidCast`
- **每次启动都弹 UAC**：给需要管理员的游戏建 HIGHEST 计划任务，向导里填 `game_task`
- **碧蓝冷启动卡登录页**：填 `launch_taps` 引导点击，或截一张标题页存为
  `title_template.png` 并开启 `launch_probe`（需要 ALAS 自带的 python + cv2）
- **日志里一堆 adb offline**：引擎已内置三级自愈（重连 → disconnect 重连 → 重启 adb server）

## 免责声明

本项目仅用于个人设备的游戏日常自动化，请遵守各游戏用户协议；因使用本项目导致的
账号风险由使用者自行承担。

## License

MIT

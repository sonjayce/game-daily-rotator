#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
三游戏轮换启动器
================
按顺序运行 火影忍者(MaaAutoNaruto) / 明日方舟(MAA) / 碧蓝航线(ALAS) 的日常脚本。

流程:
  1. 启动 MuMu 模拟器并等待 Android 就绪
  2. adb connect
  3. 逐个游戏: 启动脚本 -> 监控日志判定完成/失败 -> 结束脚本进程
     失败则诊断分类, 可重试错误走 "关游戏->关模拟器->重启->重跑" 流程
  4. 打印汇总
  5. 关闭模拟器退出

子命令:
  run             完整轮换(默认)
  check           预检: 路径/工具/配置健康度
  setup           首次配置修复: 改 ALAS Serial、MAA 连接配置(自动备份)
  detect-packages 启动模拟器后自动识别游戏包名并写入 config.json

兼容 Python 3.7+, 仅使用标准库。
"""

import argparse
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from urllib.parse import urlencode

if getattr(sys, 'frozen', False):  # PyInstaller exe: 以 exe 所在目录为基准
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, 'logs')
GAME_LOG_DIR = os.path.join(LOG_DIR, 'games')
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
STOP_FLAG = os.path.join(BASE_DIR, 'stop.flag')      # 文件停止信号 (命令行兼容)
STATUS_FILE = os.path.join(BASE_DIR, 'status.json')  # 状态快照 (调试/外部读取)
STOP_EVENT = threading.Event()                       # 进程内停止信号 (GUI/exe)
STATUS_LISTENER = None                               # 进程内状态回调 (GUI/exe)

log = logging.getLogger('rotate')

# 全局引用当前脚本进程, 便于 Ctrl+C 时清理
CURRENT = {'proc': None, 'name': ''}
LOGFILE = ''

ADVICE = {
    'OK': '',
    'TIMEOUT': '任务超时: 可能游戏卡死/弹窗/自动更新, 已重启环境重试',
    'PROCESS_EXITED': '脚本进程提前退出: 查看子日志定位崩溃原因',
    'FAILED_MARKER': '脚本报告任务失败: 查看子日志中失败标记的上下文',
    'EMULATOR_NOT_READY': '模拟器未能就绪: 检查 MuMu 是否可正常手动启动',
    'GAME_NOT_LAUNCHED': '游戏 APP 启动失败: 检查游戏是否已安装/包名是否正确, 将重启环境重试',
    'MAINTENANCE': '日志检测到维护/更新提示: 本轮跳过, 维护结束后再跑',
    'NON_RETRYABLE': '需人工介入: 详见子日志 (该错误不自动重试)',
    'MANUAL_STOP': '在 ALAS 界面手动停止: 跳过该游戏, 其余游戏继续',
}

# ---------------- GUI 联动: 实时状态 / 停止请求 ----------------

STATUS = {'phase': 'idle', 'current': '', 'attempt': 0, 'watch_file': '',
          'started_at': '', 'games': []}


def write_status(phase=None, **kw):
    """更新状态快照: 写 status.json + 通知进程内监听者 (GUI)"""
    if phase is not None:
        STATUS['phase'] = phase
    STATUS.update(kw)
    STATUS['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        atomic_write_text(STATUS_FILE,
                          json.dumps(STATUS, ensure_ascii=False, indent=1))
    except OSError:
        pass
    if STATUS_LISTENER:
        try:
            snap = dict(STATUS)
            snap['games'] = [dict(g) for g in STATUS['games']]
            STATUS_LISTENER(snap)
        except Exception:  # noqa: BLE001
            pass


def update_game_status(gid, **kw):
    for g in STATUS['games']:
        if g['id'] == gid:
            g.update(kw)
            break
    write_status()


def stop_requested():
    return STOP_EVENT.is_set() or os.path.exists(STOP_FLAG)


# ---------------- 引擎单实例锁 ----------------

LOCK_FILE = os.path.join(BASE_DIR, 'engine.lock')


def _pid_alive(pid):
    rc, out, _ = run_cmd(['tasklist', '/FI', 'PID eq %s' % pid, '/NH'],
                         timeout=15)
    return rc == 0 and str(pid) in out


def acquire_engine_lock():
    """防 GUI 与命令行同时启动两个引擎互相打架"""
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE) as f:
                content = f.read().strip()
            pid = int(content.split(':')[0])
            if _pid_alive(pid):
                return False, content
        except (ValueError, OSError):
            pass
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass
    with open(LOCK_FILE, 'w') as f:
        f.write('%d:%s' % (os.getpid(),
                           datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    return True, ''


def release_engine_lock():
    try:
        if os.path.exists(LOCK_FILE):
            with open(LOCK_FILE) as f:
                if f.read().strip().split(':')[0] == str(os.getpid()):
                    os.remove(LOCK_FILE)
    except OSError:
        pass


# ---------------- 远程推送 ----------------

NOTIFY_CFG = {}
HISTORY_FILE = os.path.join(BASE_DIR, 'history.jsonl')


def push_notify(title, text):
    """按 notify 配置推送 (Server酱/PushPlus/Telegram), 失败仅告警不阻塞"""
    ch = NOTIFY_CFG.get('channel', 'serverchan')
    token = (NOTIFY_CFG.get('token') or '').strip()
    if not NOTIFY_CFG.get('enabled'):
        return False
    if not token:
        log.warning('推送已启用但 token 为空, 跳过')
        return False
    try:
        if ch == 'telegram':
            chat = (NOTIFY_CFG.get('chat_id') or '').strip()
            url = 'https://api.telegram.org/bot%s/sendMessage' % token
            body = json.dumps({'chat_id': chat, 'text': title + '\n' + text}).encode()
            req = urllib.request.Request(url, data=body,
                                         headers={'Content-Type': 'application/json'})
        elif ch == 'pushplus':
            url = 'https://www.pushplus.plus/send'
            body = json.dumps({'token': token, 'title': title,
                               'content': text, 'template': 'txt'}).encode()
            req = urllib.request.Request(url, data=body,
                                         headers={'Content-Type': 'application/json'})
        else:  # serverchan
            url = 'https://sctapi.ftqq.com/%s.send' % token
            req = urllib.request.Request(url, data=urlencode(
                {'title': title, 'desp': text}).encode())
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
        log.info('推送成功: %s' % title)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning('推送失败 (%s): %r' % (ch, e))
        return False


# ---------------- 基础工具 ----------------

def dec(b):
    """bytes -> str, 先 utf-8 后 gbk"""
    if isinstance(b, str):
        return b
    for enc in ('utf-8', 'gbk'):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode('utf-8', errors='replace')


def run_cmd(args, timeout=30):
    """运行命令, 返回 (returncode, stdout, stderr), 输出为 str"""
    try:
        p = subprocess.run([str(a) for a in args], timeout=timeout,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return p.returncode, dec(p.stdout), dec(p.stderr)
    except FileNotFoundError:
        return -1, '', 'file not found: %s' % args[0]
    except subprocess.TimeoutExpired:
        return -2, '', 'timeout: %s' % ' '.join(str(a) for a in args)
    except Exception as e:  # noqa: BLE001
        return -3, '', repr(e)


def atomic_write_text(path, text, encoding='utf-8'):
    """原子写: 先写临时文件再替换, 避免读方(主进程/GUI)读到半截内容"""
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w', encoding=encoding) as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def jsonload(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def jsonsave(path, data):
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def backup(path):
    if os.path.exists(path):
        dst = path + '.bak-' + datetime.now().strftime('%Y%m%d_%H%M%S')
        shutil.copy2(path, dst)
        log.info('已备份: %s -> %s' % (path, dst))


# 首次运行自动生成的配置骨架 (games 为空, 填好 emulator 后在界面里添加游戏)
DEFAULT_CONFIG = {
    '_hint': '首次使用: 1) 填 emulator 的 mumu_manager/adb 路径 2) 界面「添加游戏」'
             '向导加入脚本 3) 开始轮换。字段说明见 README',
    'notify': {'enabled': False, 'channel': 'serverchan',
               'token': '', 'chat_id': ''},
    'schedule': {'enabled': False, 'time': '03:30', 'autostart': False,
                 'last_run_date': '', 'justEnabled': False},
    'emulator': {
        'mumu_manager': '',
        'mumu_player_exe': '',
        'adb': '',
        'adb_serial': '127.0.0.1:16384',
        'vm_index': 0,
        'start_timeout_sec': 180,
        'poll_interval_sec': 5,
        'shutdown_timeout_sec': 90,
    },
    'rotation': {
        'order': [],
        'max_retries': 2,
        'retry_wait_sec': 10,
        'launch_game_before': True,
        'launch_timeout_sec': 60,
        'close_game_after': True,
        'close_emulator_after': True,
        'mute_emulator': True,
        'second_pass': True,
        'parallel': True,
    },
    'games': {},
    'window': {},
}


def load_config():
    if not os.path.exists(CONFIG_PATH):
        atomic_write_text(CONFIG_PATH, json.dumps(DEFAULT_CONFIG,
                                                  ensure_ascii=False, indent=2))
        log.info('配置文件不存在, 已生成初始骨架: %s' % CONFIG_PATH)
        log.info('下一步: 填写 emulator 路径, 然后在界面「添加游戏」向导中加入脚本')
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_config(cfg):
    atomic_write_text(CONFIG_PATH,
                      json.dumps(cfg, ensure_ascii=False, indent=2))


# ---------------- 游戏类型 schema (傻瓜式添加/校验共用) ----------------
# 存储层只有三种 type: marker / alas / mxu。其中 alas 按 mode 分 ui / headless。
# 向导的模板键与存储结构一一对应: kind = marker | alas-ui | alas-headless | mxu。

GAME_TYPES = {
    'marker': {
        'label': '通用脚本 (监控日志完成标记)',
        'required': ['exe', 'log_file'],
        'files': ['exe', 'log_file'],
        'optional_defaults': {
            'type': 'marker',
            'args': '',
            'package': '',
            'package_keywords': [],
            'timeout_min': 60,
            'success_grace_sec': 20,
            'success_markers': [],
            'fail_markers': [],
            'launch_game': True,
            'launch_probe': False,
            'launch_taps': [],
            'needs_emulator': True,
            'recovery_mode': 'full',
        },
    },
    'alas-ui': {
        'label': 'ALAS 界面 (Alas.exe, 可随时干预)',
        'required': ['ui_exe'],
        'files': ['ui_exe'],
        'dirs': ['log_dir'],
        'optional_defaults': {
            'type': 'alas',
            'mode': 'ui',
            'ui_port': 22267,
            'instance': 'alas',
            'keep_ui': True,
            'ui_launch_timeout_sec': 120,
            'stall_sec': 600,
            'log_dir': '',
            'dir': '',
            'package': '',
            'package_keywords': [],
            'timeout_min': 120,
            'idle_sec': 180,
            'fail_markers': [],
            'non_retryable_markers': [],
            'launch_game': True,
            'launch_taps': [],
            'launch_probe': True,
            'needs_emulator': True,
            'recovery_mode': 'full',
        },
    },
    'alas-headless': {
        'label': 'ALAS 无头 (alas.py, 后台运行)',
        'required': ['dir', 'python', 'entry'],
        'files': ['python', 'entry'],
        'dirs': ['dir'],
        'optional_defaults': {
            'type': 'alas',
            'timeout_min': 120,
            'idle_sec': 180,
            'fail_markers': [],
            'non_retryable_markers': [],
            'package': '',
            'package_keywords': [],
            'launch_game': True,
            'launch_taps': [],
            'launch_probe': True,
            'needs_emulator': True,
            'recovery_mode': 'full',
        },
    },
    'mxu': {
        'label': 'MaaEnd / PC 端 (UIA 点「开始任务」)',
        'required': ['exe'],
        'files': ['exe'],
        'dirs': ['log_dir'],
        'optional_defaults': {
            'type': 'mxu',
            'args': '',
            'dir': '',
            'note': '',
            'launch_game': False,
            'ui_wait_sec': 12,
            'start_button': '开始任务',
            'env': {},
            'log_dir': '',
            'success_markers': [],
            'fail_markers': [],
            'started_markers': ['开始执行任务'],
            'timeout_min': 90,
            'task_start_wait_sec': 120,
            'needs_emulator': False,
            'recovery_mode': 'light',
            'game_task': '',
            'game_process': '',
            'game_wait_sec': 180,
            'game_settle_sec': 20,
        },
    },
}


def entry_kind(game):
    """存储条目 -> 模板键 (marker/alas-ui/alas-headless/mxu)"""
    t = (game or {}).get('type') or 'marker'
    if t == 'alas':
        return 'alas-ui' if (game or {}).get('mode') == 'ui' else 'alas-headless'
    if t == 'mxu':
        return 'mxu'
    return 'marker'


def defaults_for_type(kind):
    """给新条目补齐默认字段 (name/enabled 由调用方合并后注入)"""
    spec = GAME_TYPES.get(kind, GAME_TYPES['marker'])
    d = {'name': '', 'enabled': True}
    d.update(spec['optional_defaults'])
    return d


def validate_game(gid, game):
    """校验单个游戏条目, 返回结构化错误列表 (空 = 通过)。
    name/type 必填; 按类型校验必填字段与文件/目录存在性; timeout_min>0。"""
    errs = []
    if not isinstance(game, dict):
        return ['%s: 条目不是对象' % gid]
    if not str(game.get('name', '')).strip():
        errs.append('%s: 缺少 name' % gid)
    t = game.get('type')
    if t not in ('marker', 'alas', 'mxu'):
        errs.append('%s: type 非法: %r (应为 marker / alas / mxu)' % (gid, t))
        return errs
    kind = entry_kind(game)
    spec = GAME_TYPES[kind]
    for f in spec['required']:
        if game.get(f) in (None, ''):
            errs.append('%s: 缺少必填字段 %s' % (gid, f))
    for f in spec.get('files', []):
        v = game.get(f)
        if not v:
            continue
        if f == 'log_file':
            # 日志文件常在首次运行后才出现 (且可能带 {today} 占位): 只查父目录
            resolved = str(v).replace('{today}', datetime.now().strftime('%Y%m%d'))
            parent = os.path.dirname(resolved)
            if parent and not os.path.isdir(parent):
                errs.append('%s: 日志目录不存在: %s' % (gid, parent))
            continue
        if not os.path.isfile(v):
            errs.append('%s: 文件不存在 %s=%s' % (gid, f, v))
    for f in spec.get('dirs', []):
        v = game.get(f)
        if v and not os.path.isdir(v):
            errs.append('%s: 目录不存在 %s=%s' % (gid, f, v))
    tm = game.get('timeout_min', 0)
    try:
        if int(tm) <= 0:
            errs.append('%s: timeout_min 必须 > 0' % gid)
    except (TypeError, ValueError):
        errs.append('%s: timeout_min 非法: %r' % (gid, tm))
    return errs


def fill_derived(game):
    """补齐可以从其他字段推导出来的路径 (避免向导里多填一项)"""
    g = dict(game)
    t = g.get('type')
    if t == 'alas':
        if not g.get('dir'):
            base = g.get('ui_exe') or g.get('entry') or g.get('python') or ''
            if base:
                g['dir'] = os.path.dirname(os.path.abspath(base))
                if g.get('mode') != 'ui' and os.path.basename(g['dir']).lower() \
                        == 'toolkit':
                    g['dir'] = os.path.dirname(g['dir'])
    elif t == 'mxu':
        if not g.get('dir') and g.get('exe'):
            g['dir'] = os.path.dirname(os.path.abspath(g['exe']))
        if not g.get('log_dir') and g.get('dir'):
            d = os.path.join(g['dir'], 'debug')
            if os.path.isdir(d):
                g['log_dir'] = d
    return g


def add_game(cfg, gid, game, kind=None):
    """新增游戏: 校验 -> 注入默认值 -> 写入 games + 追加 order -> save_config。
    返回 (ok, errs)"""
    gid = str(gid).strip()
    if not gid:
        return False, ['游戏 id 不能为空']
    if not isinstance(game, dict):
        return False, ['游戏条目不是对象']
    if kind is None:
        kind = entry_kind(game)
    merged = defaults_for_type(kind)
    merged.update({k: v for k, v in game.items() if k not in ('id',)})
    merged['type'] = GAME_TYPES[kind]['optional_defaults'].get(
        'type', kind if kind in ('marker', 'mxu') else 'alas')
    merged = fill_derived(merged)
    errs = validate_game(gid, merged)
    if errs:
        return False, errs
    cfg.setdefault('games', {})
    cfg['games'][gid] = merged
    cfg.setdefault('rotation', {})
    order = cfg['rotation'].setdefault('order', [])
    if gid not in order:
        order.append(gid)
    save_config(cfg)
    return True, []


def update_game(cfg, gid, game):
    """更新已有游戏: 合并覆盖 -> 校验 -> 写回 (不动 order)。返回 (ok, errs)"""
    gid = str(gid).strip()
    if gid not in cfg.get('games', {}):
        return False, ['游戏不存在: %s' % gid]
    if not isinstance(game, dict):
        return False, ['游戏条目不是对象']
    merged = dict(cfg['games'][gid])
    merged.update({k: v for k, v in game.items() if k not in ('id',)})
    merged = fill_derived(merged)
    errs = validate_game(gid, merged)
    if errs:
        return False, errs
    cfg['games'][gid] = merged
    save_config(cfg)
    return True, []


def remove_game(cfg, gid):
    """删除游戏: 从 games + rotation.order 移除 -> save_config。返回 (ok, errs)
    删除前自动备份 config.json (误删可从 .bak-* 恢复)"""
    gid = str(gid).strip()
    if gid not in cfg.get('games', {}):
        return False, ['游戏不存在: %s' % gid]
    backup(CONFIG_PATH)
    del cfg['games'][gid]
    order = cfg.get('rotation', {}).get('order', [])
    if gid in order:
        cfg['rotation']['order'] = [g for g in order if g != gid]
    save_config(cfg)
    return True, []


def setup_logger():
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(GAME_LOG_DIR, exist_ok=True)
    logfile = os.path.join(
        LOG_DIR, 'rotate-%s.log' % datetime.now().strftime('%Y%m%d_%H%M%S'))
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
    fh = logging.FileHandler(logfile, encoding='utf-8')
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if sys.stdout:  # pythonw 下 stdout 为 None
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.INFO)
        ch.setFormatter(fmt)
        log.addHandler(ch)
    global LOGFILE
    LOGFILE = logfile
    return logfile


def fmt_elapsed(sec):
    sec = int(sec)
    if sec >= 3600:
        return '%dh%02dm%02ds' % (sec // 3600, sec % 3600 // 60, sec % 60)
    return '%dm%02ds' % (sec // 60, sec % 60)


def write_sublog(path, chunks):
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(''.join(chunks))
    except OSError as e:
        log.warning('子日志写入失败 %s: %s' % (path, e))


# ---------------- 模拟器控制 ----------------

class EmulatorCtl(object):
    def __init__(self, ecfg):
        self.manager = ecfg['mumu_manager']
        self.player_exe = ecfg.get('mumu_player_exe', '')
        self.adb = ecfg['adb']
        self.serial = ecfg['adb_serial']
        self.vm = str(ecfg.get('vm_index', 0))
        self.start_timeout = int(ecfg.get('start_timeout_sec', 180))
        self.poll = int(ecfg.get('poll_interval_sec', 5))

    def info(self):
        rc, out, err = run_cmd([self.manager, 'info', '-v', self.vm])
        if rc != 0:
            return None
        m = re.search(r'\{.*\}', out, re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except ValueError:
            return None

    def control(self, *args, **kw):
        timeout = kw.get('timeout', 30)
        return run_cmd([self.manager, 'control', '-v', self.vm] + list(args), timeout)

    def is_android_started(self):
        d = self.info()
        if d is None:
            return False, 'MuMuManager 无输出(模拟器可能未运行)'
        if d.get('is_android_started') is True:
            return True, 'Android 已启动'
        if d.get('is_process_started') is False:
            return False, '模拟器未运行'
        return False, '进程存在但 Android 未就绪'

    def start(self):
        ok, why = self.is_android_started()
        if ok:
            log.info('模拟器已在运行')
            return True
        log.info('启动模拟器: MuMuManager control -v %s launch' % self.vm)
        self.control('launch')
        deadline = time.time() + self.start_timeout
        while time.time() < deadline:
            time.sleep(self.poll)
            write_status()  # 长操作心跳, 防止 UI 判定引擎已死
            ok, why = self.is_android_started()
            log.debug('等待模拟器就绪: %s' % why)
            if ok:
                log.info('模拟器就绪 (Android 已启动)')
                return True
        log.error('模拟器启动超时(%ss): %s' % (self.start_timeout, why))
        return False

    def shutdown(self, timeout=90):
        log.info('关闭模拟器...')
        self.control('shutdown')
        deadline = time.time() + timeout
        while time.time() < deadline:
            write_status()  # 长操作心跳
            d = self.info()
            if d is None or d.get('is_process_started') is not True:
                log.info('模拟器已关闭')
                return True
            time.sleep(3)
        log.warning('等待模拟器退出超时, 继续后续流程')
        return False

    def _device_state(self):
        """返回 serial 在 adb devices 中的状态: device/offline/unauthorized/''"""
        _, out2, _ = run_cmd([self.adb, 'devices'], timeout=15)
        for line in out2.splitlines():
            line = line.strip()
            if line.startswith(self.serial):
                parts = line.split()
                if len(parts) >= 2:
                    return parts[1]
        return ''

    def _ensure_adb(self):
        """确保 adb 设备可用; 脚本退出可能杀掉 adb server, 需自动重连"""
        state = self._device_state()
        if state == 'device':
            return True
        log.debug('ADB 未就绪(状态=%r), 尝试重连...' % state)
        run_cmd([self.adb, 'connect', self.serial], timeout=15)
        return self._device_state() == 'device'

    def adb_connect(self):
        run_cmd([self.adb, 'connect', self.serial], timeout=15)
        state = self._device_state()
        if state == 'device':
            log.info('ADB 已连接 %s' % self.serial)
            return True
        # 模拟器重启后 adb server 常留 stale offline 条目: 先断开再重连
        if state in ('offline', 'unauthorized'):
            log.warning('ADB 设备状态 %s, 尝试 disconnect 后重连...' % state)
            run_cmd([self.adb, 'disconnect', self.serial], timeout=15)
            time.sleep(2)
            run_cmd([self.adb, 'connect', self.serial], timeout=15)
            if self._device_state() == 'device':
                log.info('ADB 重连成功 %s' % self.serial)
                return True
            # 仍失败: 重启 adb server (会同时断开其他工具的连接, 各工具会自行重连)
            log.warning('仍 offline, 重启 adb server...')
            run_cmd([self.adb, 'kill-server'], timeout=15)
            time.sleep(2)
            run_cmd([self.adb, 'connect', self.serial], timeout=15)
            if self._device_state() == 'device':
                log.info('ADB 重启 server 后连接成功 %s' % self.serial)
                return True
        log.error('ADB 连接失败: 设备状态=%r' % state)
        return False

    def app_running(self, pkg):
        """游戏进程是否在运行 (pidof 有输出)"""
        if not pkg:
            return False
        if not self._ensure_adb():
            return False
        rc, out, _ = run_cmd([self.adb, '-s', self.serial, 'shell',
                              'pidof', pkg], timeout=15)
        return rc == 0 and bool(out.strip())

    def app_launch(self, pkg):
        if not pkg:
            return
        log.info('启动游戏 APP: %s' % pkg)
        rc, out, err = self.control('app', 'launch', '-pkg', pkg)
        if rc != 0:
            log.warning('MuMuManager app launch 失败(%r %r), 改用 adb monkey'
                        % (out.strip(), err.strip()))
            if self._ensure_adb():
                run_cmd([self.adb, '-s', self.serial, 'shell', 'monkey',
                         '-p', pkg, '-c', 'android.intent.category.LAUNCHER',
                         '1'], timeout=20)

    def wait_app(self, pkg, timeout=60):
        """轮询等待游戏进程起来, 返回是否成功"""
        if not pkg:
            return True
        deadline = time.time() + timeout
        hb = 0.0
        while time.time() < deadline:
            if time.time() - hb > 15:  # 心跳: 长阻塞期避免 UI 误判引擎已死
                hb = time.time()
                write_status()
            if self.app_running(pkg):
                log.info('游戏进程已就绪: %s' % pkg)
                return True
            time.sleep(3)
        log.warning('等待游戏进程超时(%ss): %s (脚本仍将继续, 由其自身逻辑处理)'
                    % (timeout, pkg))
        return False

    def app_close(self, pkg):
        if not pkg:
            return
        log.info('关闭游戏 APP: %s' % pkg)
        self.control('app', 'close', '-pkg', pkg)
        # adb 兜底 (adb server 可能被脚本退出杀掉, 先自愈)
        if self._ensure_adb():
            run_cmd([self.adb, '-s', self.serial, 'shell', 'am',
                     'force-stop', pkg], timeout=20)


# ---------------- 进程/日志监控 ----------------

class Proc(object):
    """Popen 包装, 支持结束整个进程树"""

    def __init__(self, exe_args, cwd=None, stdout_path=None, env=None):
        self._fout = open(stdout_path, 'wb') if stdout_path else None
        self.p = subprocess.Popen(
            exe_args, cwd=cwd, env=env,
            stdout=(self._fout if self._fout else subprocess.DEVNULL),
            stderr=subprocess.STDOUT)

    def poll(self):
        return self.p.poll()

    def kill_tree(self):
        run_cmd(['taskkill', '/PID', str(self.p.pid), '/T', '/F'], timeout=20)
        try:
            self.p.wait(timeout=15)
        except Exception:  # noqa: BLE001
            pass

    def close(self):
        if self._fout:
            try:
                self._fout.close()
            except OSError:
                pass


class Tail(object):
    """只读增量跟踪一个日志文件"""

    def __init__(self, path):
        self.path = path
        self.offset = os.path.getsize(path) if path and os.path.exists(path) else 0

    def poll(self):
        """返回自上次以来的新增文本(可能为空), 并推进偏移"""
        if not self.path or not os.path.exists(self.path):
            return ''
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return ''
        if size < self.offset:  # 日志被轮转/重建
            self.offset = 0
        if size == self.offset:
            return ''
        data = b''
        with open(self.path, 'rb') as f:
            f.seek(self.offset)
            data = f.read()
        self.offset += len(data)
        return dec(data)


class GameResult(object):
    def __init__(self, status, category='OK', detail='', sublog=''):
        self.status = status          # SUCCESS / FAILED / SKIPPED
        self.category = category      # 见 ADVICE
        self.detail = detail
        self.sublog = sublog


# ---------------- Runner: 标记型(火影 / MAA) ----------------

def resolve_log_path(game):
    p = game['log_file']
    if '{today}' in p:
        p = p.replace('{today}', datetime.now().strftime('%Y%m%d'))
    return p


def run_marker_game(game, emu, attempt, ts):
    """启动 GUI 脚本, 通过增量扫描日志找成功/失败标记"""
    gid = game['id']
    log_path = resolve_log_path(game)
    sublog = os.path.join(GAME_LOG_DIR, '%s_%s_try%d.log' % (gid, ts, attempt))
    tail = Tail(log_path)
    write_status(watch_file=log_path)

    args = [a for a in str(game.get('args', '')).split() if a]
    exe_args = [game['exe']] + args
    # 单实例应用清场: 残留实例会把新启动重定向后退出, 导致监控扑空
    exe_name = os.path.basename(game['exe'])
    rc, out, _ = run_cmd(['tasklist', '/FI', 'IMAGENAME eq %s' % exe_name,
                          '/NH'], timeout=15)
    if exe_name.lower() in out.lower():
        log.info('[%s] 检测到残留 %s, 先结束再启动' % (gid, exe_name))
        run_cmd(['taskkill', '/IM', exe_name, '/T', '/F'], timeout=20)
        time.sleep(2)
    log.info('[%s] 启动: %s %s' % (gid, game['exe'], ' '.join(args)))
    proc = Proc(exe_args, cwd=game.get('cwd') or os.path.dirname(game['exe']))
    CURRENT['proc'] = proc
    CURRENT['name'] = gid

    timeout = int(game.get('timeout_min', 60)) * 60
    grace = int(game.get('success_grace_sec', 20))
    success_m = game.get('success_markers', [])
    fail_m = game.get('fail_markers', [])

    status, cat, detail = 'FAILED', 'PROCESS_EXITED', '未知'
    seen_success = False
    t_success = None
    chunks = []
    start = time.time()
    hb = time.time()
    try:
        while True:
            time.sleep(3)
            if time.time() - hb > 30:  # 心跳: 让 GUI 确认轮换进程存活
                hb = time.time()
                write_status()
            # 跨天日志轮转 (火影/MAA 按日期分文件)
            new_path = resolve_log_path(game)
            if new_path != tail.path and os.path.exists(new_path):
                log.info('[%s] 日志轮转, 切换到 %s'
                         % (gid, os.path.basename(new_path)))
                tail = Tail(new_path)
                write_status(watch_file=new_path)
            if stop_requested():
                status, cat, detail = 'STOPPED', 'STOPPED', '用户停止请求'
                break
            new = tail.poll()
            if new:
                chunks.append(new)
                if not seen_success and success_m and \
                        any(m in new for m in success_m):
                    seen_success = True
                    t_success = time.time()
                    log.info('[%s] 检测到完成标记 (等待 %ss 后收尾)'
                             % (gid, grace))
                if not seen_success:
                    hit = next((m for m in fail_m if m in new), None)
                    if hit:
                        status = 'FAILED'
                        cat = 'FAILED_MARKER'
                        detail = '日志出现失败标记: %s' % hit
                        break
            rc = proc.poll()
            if rc is not None:
                new = tail.poll()
                if new:
                    chunks.append(new)
                if seen_success:
                    status, cat, detail = 'SUCCESS', 'OK', '进程退出, 此前已见完成标记'
                else:
                    status = 'FAILED'
                    cat = 'PROCESS_EXITED'
                    detail = '脚本进程退出(rc=%s)且未见完成标记' % rc
                break
            if seen_success and time.time() - t_success >= grace:
                status = 'SUCCESS'
                cat = 'OK'
                detail = '完成标记命中'
                break
            if time.time() - start > timeout:
                status = 'FAILED'
                cat = 'TIMEOUT'
                detail = '超过硬超时 %s 分钟' % game.get('timeout_min')
                break
    finally:
        log.info('[%s] 结束脚本进程树...' % gid)
        proc.kill_tree()
        proc.close()
        CURRENT['proc'] = None
        write_sublog(sublog, chunks)

    res = GameResult(status, cat, detail, sublog)
    res.chunks = chunks
    return res


# ---------------- Runner: ALAS(碧蓝航线) ----------------

def alas_log_file(game, kind='alas'):
    """ALAS 自己的日志: log/<今天>_alas.txt (任务) / log/<今天>_gui.txt (界面事件)"""
    d = game.get('log_dir') or os.path.join(game.get('dir', ''), 'log')
    return os.path.join(d, '%s_%s.txt' % (datetime.now().strftime('%Y-%m-%d'),
                                          kind))


def _port_open(host, port, timeout=0.6):
    """本机端口是否已在监听 (判断 ALAS 界面是否已启动)"""
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, int(port))) == 0
    finally:
        s.close()


def ensure_alas_run_on_start(game):
    """让 ALAS 界面一启动就自动跑该实例: config/deploy.yaml 里 Webui.Run = 实例名
    (ALAS 的 deploy 配置是按行读的, 缩进无意义, 改值即可)"""
    d = game.get('dir') or ''
    inst = game.get('instance', 'alas')
    if not d:
        return False
    p = os.path.join(d, 'config', 'deploy.yaml')
    try:
        with open(p, 'r', encoding='utf-8') as f:
            text = f.read()
    except OSError:
        text = ''
    pat_run = re.compile(r'(?m)^\s*Run:\s*[\'"]?\[?[\'"]?%s[\'"]?\]?[\'"]?\s*$'
                         % re.escape(inst))
    if text and pat_run.search(text):
        return True
    try:
        if not text:
            atomic_write_text(p, 'Deploy:\n  Webui:\n    Run: %s\n' % inst)
        else:
            backup(p)
            if re.search(r'(?m)^\s*Run:', text):
                text = re.sub(r'(?m)^(\s*)Run:.*$', r'\1Run: %s' % inst,
                              text, count=1)
            elif re.search(r'(?m)^\s*WebuiHost:', text):
                text = re.sub(
                    r'(?m)^(\s*)WebuiHost:.*$',
                    lambda m: m.group(0) + '\n%sRun: %s' % (m.group(1), inst),
                    text, count=1)
            else:
                text = text.rstrip('\n') + '\nRun: %s\n' % inst
            atomic_write_text(p, text)
        log.info('ALAS config/deploy.yaml 已设置 Run: %s (界面启动即自动开跑)'
                 % inst)
        return True
    except OSError as e:
        log.warning('deploy.yaml 写入失败: %s' % e)
        return False


def _port_owner_pid(port):
    """监听该端口的进程 PID (通常是 ALAS 界面的 python gui.py)"""
    rc, out, _ = run_cmd(['netstat', '-ano'], timeout=20)
    if rc != 0:
        return 0
    suffix = ':%d' % int(port)
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == 'TCP' \
                and parts[1].endswith(suffix) and parts[3].upper() == 'LISTENING':
            try:
                return int(parts[4])
            except ValueError:
                pass
    return 0


def stop_alas_ui(port):
    """结束 ALAS 界面进程树 (Alas.exe 只是启动器, 真正的界面是监听端口的 gui.py)"""
    pid = _port_owner_pid(port)
    if not pid:
        return False
    log.info('结束 ALAS 界面 (PID %d, 端口 %s)' % (pid, port))
    run_cmd(['taskkill', '/PID', str(pid), '/T', '/F'], timeout=20)
    return True


def run_alas_ui_game(game, emu, attempt, ts):
    """ALAS 界面模式: 启动 Alas.exe, 界面自动拉起 alas 实例, 可随时手动干预

    判定信号全部来自 ALAS 自己的日志:
      任务日志 log/<今天>_alas.txt → 'Wait until ... for task' + 空闲 → 完成
      GUI 日志 log/<今天>_gui.txt  → '[alas] exited'           → 人工停止(STOPPED)
                                   → 之后 'Starting [alas]'    → 更新/重载重启, 继续等
    """
    gid = game['id']
    ui_exe = game.get('ui_exe') or ''
    port = int(game.get('ui_port', 22267))
    inst = game.get('instance', 'alas')
    task_log = alas_log_file(game, 'alas')
    gui_log = alas_log_file(game, 'gui')

    timeout = int(game.get('timeout_min', 120)) * 60
    idle_sec = int(game.get('idle_sec', 180))
    launch_timeout = int(game.get('ui_launch_timeout_sec', 120))
    stall_sec = int(game.get('stall_sec', 600))
    fail_m = game.get('fail_markers', [])
    non_retry_m = game.get('non_retryable_markers', [])
    stop_pat = '[%s] exited' % inst
    restart_pat = 'Starting [%s]' % inst

    if not ui_exe or not os.path.isfile(ui_exe):
        return GameResult('FAILED', 'GAME_NOT_LAUNCHED',
                          'ALAS 界面程序不存在: %s' % ui_exe, task_log)

    ensure_alas_run_on_start(game)

    proc = None
    if _port_open('127.0.0.1', port):
        log.info('[%s] 检测到 ALAS 已在运行 (端口 %d), 复用现有界面' % (gid, port))
    else:
        log.info('[%s] 启动 ALAS 界面: %s' % (gid, ui_exe))
        proc = Proc([ui_exe], cwd=game.get('dir') or os.path.dirname(ui_exe))
    CURRENT['ui_proc'] = proc
    CURRENT['ui_port'] = port
    CURRENT['proc'] = proc
    CURRENT['name'] = gid
    write_status(watch_file=task_log)

    tail = Tail(task_log)
    gtail = Tail(gui_log)
    status, cat, detail = 'FAILED', 'PROCESS_EXITED', '未知'
    idle_ok = False
    seen_output = False
    stop_events = 0
    port_closed = 0
    start = time.time()
    last_activity = start
    hb = start
    try:
        while True:
            time.sleep(3)
            if time.time() - hb > 30:  # 心跳
                hb = time.time()
                write_status()
            if stop_requested():
                status, cat, detail = 'STOPPED', 'STOPPED', '用户停止请求'
                break

            # 界面事件: 人工停止 / 更新重载
            g = gtail.poll()
            if g:
                for line in g.splitlines():
                    if stop_pat in line:
                        stop_events += 1
                        log.warning('[%s] ALAS 界面报告实例停止: %s'
                                    % (gid, line.strip()))
                    elif restart_pat in line:
                        stop_events = 0
                        log.info('[%s] ALAS 实例重新启动 (更新/重载), 继续监控'
                                 % gid)
            if stop_events:
                # 只是这一个游戏被你在界面里停了: 跳过它继续跑后面的, 不联动整轮停止
                status, cat, detail = 'SKIPPED', 'MANUAL_STOP', \
                    '在 ALAS 界面手动停止 (跳过该游戏, 界面保留)'
                log.warning('[%s] 检测到手动停止, 跳过该游戏 (整轮继续)' % gid)
                break

            new = tail.poll()
            if new:
                seen_output = True
                last_activity = time.time()
                for line in new.splitlines():
                    if 'Wait until' in line and 'for task' in line:
                        idle_ok = True
                        log.info('[%s] 调度器进入空闲等待 (判定为日常完成候选)'
                                 % gid)
                    hit = next((m for m in fail_m if m in line), None)
                    if hit:
                        if any(m in line for m in non_retry_m):
                            status, cat = 'SKIPPED', 'NON_RETRYABLE'
                        else:
                            status, cat = 'FAILED', 'FAILED_MARKER'
                        detail = '日志出现失败标记: %s' % hit
                        break
                if cat in ('NON_RETRYABLE', 'FAILED_MARKER'):
                    break

            if idle_ok and time.time() - last_activity >= idle_sec:
                status, cat, detail = 'SUCCESS', 'OK', \
                    '调度器空闲 %ss, 判定全部任务完成' % idle_sec
                break
            # 注意: Alas.exe 只是启动器 (启动后自行退出), 真正的界面是监听端口的 gui.py,
            # 所以不能用启动器的退出码判断 ALAS 是否还活着, 要看端口 + 日志活性。
            if seen_output:
                if _port_open('127.0.0.1', port):
                    port_closed = 0
                else:
                    port_closed += 1
                    if port_closed >= 2:
                        if idle_ok:
                            status, cat, detail = 'SUCCESS', 'OK', \
                                '界面已关闭, 此前已判定空闲'
                        else:
                            status, cat, detail = 'FAILED', 'PROCESS_EXITED', \
                                'ALAS 界面已关闭 (窗口被关/崩溃, 未见完成标记)'
                        break
            if not seen_output and time.time() - start > launch_timeout:
                status, cat, detail = 'FAILED', 'GAME_NOT_LAUNCHED', \
                    'ALAS 界面启动后 %ss 内无任务日志: %s' % (launch_timeout,
                                                             task_log)
                break
            if time.time() - last_activity > stall_sec:
                status, cat, detail = 'FAILED', 'PROCESS_EXITED', \
                    'ALAS 无输出超过 %ss (可能被外部结束)' % stall_sec
                break
            if time.time() - start > timeout:
                status, cat, detail = 'FAILED', 'TIMEOUT', \
                    '超过硬超时 %s 分钟' % game.get('timeout_min')
                break
    finally:
        keep = bool(game.get('keep_ui', True)) and (
            status in ('SUCCESS', 'STOPPED') or cat == 'MANUAL_STOP')
        if not keep:
            log.info('[%s] 结束 ALAS 界面进程树...' % gid)
            stop_alas_ui(port)
            if proc is not None:
                proc.close()
            CURRENT['ui_proc'] = None
        else:
            log.info('[%s] ALAS 界面保留, 可在界面里查看/继续操作' % gid)
        CURRENT['proc'] = None
        if not os.path.exists(task_log):
            write_sublog(os.path.join(
                GAME_LOG_DIR, '%s_%s_try%d.log' % (gid, ts, attempt)),
                ['(ALAS 界面模式, 任务日志: %s)\n' % task_log])

    return GameResult(status, cat, detail, task_log)


def run_alas_game(game, emu, attempt, ts):
    """碧蓝航线入口: mode=ui 走界面(可干预), 否则无头调度器"""
    if game.get('mode') == 'ui':
        return run_alas_ui_game(game, emu, attempt, ts)
    return run_alas_headless_game(game, emu, attempt, ts)


def run_alas_headless_game(game, emu, attempt, ts):
    """无头运行 alas.py, 输出重定向到子日志, 空闲判定完成"""
    gid = game['id']
    sublog = os.path.join(GAME_LOG_DIR, '%s_%s_try%d.log' % (gid, ts, attempt))
    log.info('[%s] 启动: %s %s' % (gid, game['python'], game['entry']))
    proc = Proc([game['python'], game['entry']], cwd=game['dir'],
                stdout_path=sublog)
    CURRENT['proc'] = proc
    CURRENT['name'] = gid
    write_status(watch_file=sublog)

    timeout = int(game.get('timeout_min', 120)) * 60
    idle_sec = int(game.get('idle_sec', 180))
    fail_m = game.get('fail_markers', [])
    non_retry_m = game.get('non_retryable_markers', [])

    tail = Tail(sublog)  # 新文件, 从 0 开始
    idle_ok = False
    last_activity = time.time()
    start = last_activity
    hb = time.time()
    status, cat, detail = 'FAILED', 'PROCESS_EXITED', '未知'
    try:
        while True:
            time.sleep(3)
            if time.time() - hb > 30:  # 心跳
                hb = time.time()
                write_status()
            if stop_requested():
                status, cat, detail = 'STOPPED', 'STOPPED', '用户停止请求'
                break
            new = tail.poll()
            if new:
                last_activity = time.time()
                for line in new.splitlines():
                    if 'Scheduler: Start task' in line or \
                            'Scheduler: End task' in line:
                        log.debug('[%s] %s' % (gid, line.strip()))
                    if 'Wait until' in line and 'for task' in line:
                        idle_ok = True
                        log.info('[%s] 调度器进入空闲等待 (判定为日常完成候选)'
                                 % gid)
                    hit = next((m for m in fail_m if m in line), None)
                    if hit:
                        if any(m in line for m in non_retry_m):
                            status = 'SKIPPED'
                            cat = 'NON_RETRYABLE'
                        else:
                            status = 'FAILED'
                            cat = 'FAILED_MARKER'
                        detail = '日志出现失败标记: %s' % hit
                        break
                if cat in ('NON_RETRYABLE', 'FAILED_MARKER'):
                    break
            rc = proc.poll()
            if rc is not None:
                if rc == 0:
                    status, cat, detail = 'SUCCESS', 'OK', '调度器正常退出(rc=0)'
                else:
                    status = 'FAILED'
                    cat = 'PROCESS_EXITED'
                    detail = '调度器退出(rc=%s)且未完成空闲判定' % rc
                break
            if idle_ok and time.time() - last_activity >= idle_sec:
                status = 'SUCCESS'
                cat = 'OK'
                detail = '调度器空闲 %ss, 判定全部任务完成' % idle_sec
                break
            if time.time() - start > timeout:
                status = 'FAILED'
                cat = 'TIMEOUT'
                detail = '超过硬超时 %s 分钟' % game.get('timeout_min')
                break
    finally:
        log.info('[%s] 结束脚本进程树...' % gid)
        proc.kill_tree()
        proc.close()
        CURRENT['proc'] = None

    return GameResult(status, cat, detail, sublog)


# ---------------- 诊断与重试 ----------------

def ensure_ready(emu):
    ok, why = emu.is_android_started()
    if not ok:
        log.info('模拟器状态: %s' % why)
        if not emu.start():
            return False
    return emu.adb_connect()


def do_recovery(game, emu, wait):
    """恢复流程: light=只重启游戏APP(保下载进度); full=整环境重启"""
    mode = game.get('recovery_mode', 'full')
    name = game.get('name', game['id'])
    if mode == 'light':
        log.warning('[%s] 轻量恢复: 关游戏 -> 等%ds (不动模拟器)'
                    % (name, wait))
        emu.app_close(game.get('package', ''))
        time.sleep(wait)
        return True
    log.warning('[%s] 触发恢复流程: 关游戏 -> 关模拟器 -> 等%ds -> 重启模拟器'
                % (name, wait))
    emu.app_close(game.get('package', ''))
    emu.shutdown()
    time.sleep(wait)
    if not emu.start():
        log.error('恢复失败: 模拟器启动失败')
        return False
    if not emu.adb_connect():
        log.error('恢复失败: ADB 连接失败')
        return False
    log.info('恢复完成, 模拟器已就绪')
    return True


def wait_with_stop(seconds, tick=5):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if stop_requested():
            return False
        time.sleep(tick)
    return True


def log_mentions_maintenance(path):
    """日志尾部出现维护提示 → 本轮不可能成功"""
    if not path:
        return False
    try:
        with open(path, 'rb') as f:
            f.seek(max(0, os.path.getsize(path) - 150000))
            data = f.read().decode('utf-8', errors='replace')
        return '维护' in data
    except OSError:
        return False


def append_history(summary, trigger):
    """追加一条轮换历史记录 (历史看板数据源)"""
    rec = {
        'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'trigger': trigger,
        'results': [{'name': name, 'status': status, 'elapsed': round(elapsed)}
                    for name, status, _cat, elapsed, _detail in summary],
    }
    try:
        with open(HISTORY_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except OSError:
        pass


def wait_title_and_enter(emu, game):
    """智能等待: 轮询标题页出现 → 点击进入 → 关公告弹窗
    探测是尽力而为, 任何失败都走固定点击兜底"""
    py = game.get('python') or ''
    probe = os.path.join(BASE_DIR, 'probe_title.py')
    tpl = os.path.join(BASE_DIR, 'title_template.png')
    if not (py and os.path.isfile(py) and os.path.isfile(probe)
            and os.path.isfile(tpl)):
        return False
    log.info('[%s] 智能等待: 轮询标题页 (最长150s)...' % game['name'])
    deadline = time.time() + 150
    seen = False
    hb = 0.0
    while time.time() < deadline:
        if stop_requested():
            return True
        if time.time() - hb > 15:  # 心跳: 探针子进程最长 60s, 期间 UI 不能判定引擎已死
            hb = time.time()
            write_status()
        rc, out, _ = run_cmd([py, probe, emu.adb, emu.serial, tpl], timeout=60)
        if 'TITLE' in out:
            seen = True
            log.info('[%s] 标题页出现' % game['name'])
            break
        time.sleep(6)
    if not seen:
        log.info('[%s] 未探测到标题页, 直接执行引导点击' % game['name'])
    log.info('[%s] 点击进入游戏...' % game['name'])
    run_cmd([emu.adb, '-s', emu.serial, 'shell', 'input', 'tap', '640', '400'],
            timeout=15)
    time.sleep(12)
    log.info('[%s] 关闭公告弹窗...' % game['name'])
    run_cmd([emu.adb, '-s', emu.serial, 'shell', 'input', 'tap', '1204', '95'],
            timeout=15)
    time.sleep(3)
    return True


def pre_launch_game(game, emu, rot):
    """按配置在脚本启动前拉起游戏 APP 并确认进程就绪
    launch_taps: [{at: 秒, x, y}] 游戏拉起后按时间点点击屏幕, 引导跳过
    等待界面 (如碧蓝的"轻触屏幕开始"), 使脚本启动时游戏已进入可识别界面
    """
    if not rot.get('launch_game_before', True):
        return True
    if not game.get('launch_game', True):
        return True
    pkg = game.get('package', '')
    if not pkg:
        log.info('[%s] 未配置包名, 跳过游戏启动' % game['name'])
        return True
    t0 = time.time()
    if emu.app_running(pkg):
        # 已在运行的游戏无需引导点击 (taps 只对冷启动有意义)
        log.info('[%s] 游戏已在运行, 无需重复启动' % game['name'])
        return True
    emu.app_launch(pkg)
    if not emu.wait_app(pkg, int(rot.get('launch_timeout_sec', 60))):
        return False
    # 智能等待: 探测标题页→点击进入→关公告 (需 ALAS python 的 cv2)
    if game.get('launch_probe') and wait_title_and_enter(emu, game):
        return True
    # 兜底: 固定时间引导点击 (游戏加载耗时不定, 点击落在加载期则无害)
    taps = game.get('launch_taps') or []
    for t in taps:
        wait = float(t['at']) - (time.time() - t0)
        if wait > 0:
            deadline = time.time() + wait
            while time.time() < deadline:  # 等待期间响应停止请求
                if stop_requested():
                    return True
                time.sleep(1)
        log.info('[%s] 启动引导: 点击屏幕 (%s, %s)'
                 % (game['name'], t['x'], t['y']))
        run_cmd([emu.adb, '-s', emu.serial, 'shell', 'input', 'tap',
                 str(t['x']), str(t['y'])], timeout=15)
    return True


def run_mxu_game(game, emu, attempt, ts):
    """MXU(Tauri) 类应用: [可选计划任务静默启动游戏] → 启动 MXU → UIA 点开始任务 → 监控日志
    适用: 终末地(MaaEnd, PC端) / 航海王热血航线(MaaGrandLine, 模拟器端) 等 MaaXYZ MXU 形态
    (终末地游戏本体 requireAdministrator, 用 HIGHEST 计划任务绕 UAC)"""
    gid = game['id']
    import base64

    # 1 计划任务静默启动游戏 (无 UAC)
    task = game.get('game_task', '')
    if task:
        log.info('[%s] 计划任务启动游戏: %s' % (gid, task))
        run_cmd(['schtasks', '/run', '/tn', task], timeout=30)
    gp = game.get('game_process', '')
    if gp:
        deadline = time.time() + int(game.get('game_wait_sec', 180))
        while time.time() < deadline:
            if stop_requested():
                return GameResult('STOPPED', 'STOPPED', '用户停止请求')
            rc, out, _ = run_cmd(['tasklist', '/FI', 'IMAGENAME eq %s' % gp,
                                  '/NH'], timeout=15)
            if gp.lower() in out.lower():
                log.info('[%s] 游戏进程已就绪' % gid)
                break
            time.sleep(5)
        else:
            return GameResult('FAILED', 'GAME_NOT_LAUNCHED',
                              '游戏进程未出现: %s' % gp)
        time.sleep(int(game.get('game_settle_sec', 20)))

    # 2 启动 MXU 应用 (环境变量强制开启 WebView2 可访问性, UIA 才能点到按钮)
    env = dict(os.environ)
    # MuMu 目录前置到 PATH: MaaToolkit 设备枚举靠 PATH 找 MuMuManager/adb
    # (MXU 应用连模拟器时实测必需, 与游戏自带启动脚本的 PATH 设置一致)
    for d in [os.path.dirname(emu.manager), os.path.dirname(emu.adb)]:
        if d and os.path.isdir(d):
            env['PATH'] = d + os.pathsep + env.get('PATH', '')
    # 游戏自带脚本所需的额外目录 (如 MuMu nx_device\shell)
    for d in (game.get('path_prepend') or []):
        if d and os.path.isdir(d):
            env['PATH'] = d + os.pathsep + env['PATH']
    for k, v in (game.get('env') or {}).items():
        env[k] = v
    sublog = os.path.join(GAME_LOG_DIR, '%s_%s_try%d.log' % (gid, ts, attempt))
    exe = game['exe']
    args = [a for a in str(game.get('args', '')).split() if a]
    proc = Proc([exe] + args, cwd=game.get('dir') or os.path.dirname(exe),
                stdout_path=sublog, env=env)
    CURRENT['proc'] = proc
    CURRENT['name'] = gid

    # 3 UIA 点击「开始任务」(按钮名可配, 重试等待 UI 就绪)
    # 进程名默认从 exe 推导 (MaaEnd/MaaGrandLine 等), 也可用 process_name 覆盖
    time.sleep(int(game.get('ui_wait_sec', 12)))
    btn_name = game.get('start_button', '开始任务')
    proc_name = game.get('process_name', '') or \
        os.path.splitext(os.path.basename(exe))[0]
    ps = (
        '$ErrorActionPreference = "Stop"\n'
        'Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes\n'
        '$btn = $null\n'
        'foreach ($i in 1..15) {\n'
        '  $p = Get-Process -Name "' + proc_name + '" -ErrorAction SilentlyContinue | '
        'Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1\n'
        '  if ($p) {\n'
        '    try {\n'
        '      $root = [System.Windows.Automation.AutomationElement]::FromHandle($p.MainWindowHandle)\n'
        '      $cond = New-Object System.Windows.Automation.PropertyCondition('
        '[System.Windows.Automation.AutomationElement]::NameProperty, [string]"' +
        btn_name + '")\n'
        '      $btn = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)\n'
        '      if ($btn) { break }\n'
        '    } catch {}\n'
        '  }\n'
        '  Start-Sleep -Seconds 3\n'
        '}\n'
        'if (-not $btn) { Write-Host "BTN_NOT_FOUND"; exit 1 }\n'
        '$inv = $btn.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)\n'
        '$inv.Invoke()\n'
        'Write-Host "INVOKED"\n'
    )
    b64 = base64.b64encode(ps.encode('utf-16-le')).decode('ascii')
    uia_ok = False
    for i in range(4):
        if stop_requested():
            return GameResult('STOPPED', 'STOPPED', '用户停止请求')
        write_status()  # 心跳: 单次 UIA 调用最长 90s
        rc, out, err = run_cmd(['powershell', '-NoProfile', '-EncodedCommand',
                                b64], timeout=90)
        if 'INVOKED' in out:
            uia_ok = True
            log.info('[%s] UIA 已点击「%s」' % (gid, btn_name))
            break
        log.info('[%s] UIA 未找到按钮(%d/4), 重试...' % (gid, i + 1))
        time.sleep(5)
    if not uia_ok:
        log.error('[%s] UIA 点击失败, 子日志: %s' % (gid, sublog))

    # 4 监控 MXU 日志 (按 mtime 取最新 dated log)
    log_dir = game.get('log_dir') or os.path.join(os.path.dirname(exe), 'debug')
    log_path = log_path_newest(log_dir)
    if log_path:
        log.info('[%s] 监控日志: %s' % (gid, os.path.basename(log_path)))
        write_status(watch_file=log_path)
    tail = Tail(log_path if log_path else '')

    timeout = int(game.get('timeout_min', 90)) * 60
    # 任务启动确认: UIA 点击成功后, 日志里应出现「开始执行任务」。
    # 如果 UIA 失败且等待期内日志始终没有任务启动痕迹, 提前失败而非干等超时
    start_wait = int(game.get('task_start_wait_sec', 120))
    start_confirm_deadline = (time.time() + start_wait) if not uia_ok else 0
    success_m = game.get('success_markers', [])
    fail_m = game.get('fail_markers', [])
    started_m = game.get('started_markers', ['开始执行任务'])
    status, cat, detail = 'FAILED', 'PROCESS_EXITED', '未知'
    start = time.time()
    hb = time.time()
    hit_success = None
    task_started = not uia_ok  # UIA 成功时日志由引擎自行确认; 失败时额外探测
    try:
        while True:
            time.sleep(3)
            if time.time() - hb > 30:
                hb = time.time()
                write_status()
            if stop_requested():
                status, cat, detail = 'STOPPED', 'STOPPED', '用户停止请求'
                break
            # MXU 会轮转日志(2026-MM-DD-N.log): 检测到新文件就切过去
            cur_newest = log_path_newest(log_dir)
            if cur_newest and cur_newest != tail.path:
                log.info('[%s] 日志轮转, 切换到 %s'
                         % (gid, os.path.basename(cur_newest)))
                tail = Tail(cur_newest)
                write_status(watch_file=cur_newest)
            new = tail.poll()
            if new:
                if not task_started and any(m in new for m in started_m):
                    task_started = True
                    log.info('[%s] 任务已开始执行' % gid)
                if not hit_success:
                    hit_success = next((m for m in success_m if m in new), None)
                hit_f = next((m for m in fail_m if m in new), None)
                if hit_success:
                    status, cat, detail = 'SUCCESS', 'OK', '完成标记: %s' % hit_success
                    break
                if hit_f:
                    status, cat, detail = 'FAILED', 'FAILED_MARKER', \
                        '失败标记: %s' % hit_f
                    break
            # UIA 失败且等待期内日志无任务启动痕迹 → 提前失败
            if start_confirm_deadline and time.time() > start_confirm_deadline:
                if not task_started:
                    status, cat, detail = 'FAILED', 'GAME_NOT_LAUNCHED', \
                        'UIA 点击失败且 %ss 内日志无任务启动痕迹' % start_wait
                    break
                start_confirm_deadline = 0  # 已确认启动, 不再检查
            rc = proc.poll()
            if rc is not None:
                if hit_success:
                    status, cat, detail = 'SUCCESS', 'OK', '进程退出, 此前已见完成标记'
                else:
                    status, cat, detail = 'FAILED', 'PROCESS_EXITED', \
                        'MXU 进程退出(rc=%s)且未见完成标记' % rc
                break
            if time.time() - start > timeout:
                status, cat, detail = 'FAILED', 'TIMEOUT', \
                    '超过硬超时 %s 分钟' % game.get('timeout_min')
                break
    finally:
        log.info('[%s] 结束脚本进程树...' % gid)
        proc.kill_tree()
        proc.close()
        CURRENT['proc'] = None

    return GameResult(status, cat, detail, sublog)


def log_path_newest(log_dir):
    try:
        cands = [os.path.join(log_dir, n) for n in os.listdir(log_dir)
                 if re.match(r'\d{4}-\d\d-\d\d-\d+\.log$', n)]
        if not cands:
            return ''
        return max(cands, key=os.path.getmtime)
    except OSError:
        return ''


def run_game_with_retry(game, emu, rot):
    max_retry = int(rot.get('max_retries', 2))
    wait = int(rot.get('retry_wait_sec', 10))
    total = 1 + max_retry
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    t0 = time.time()
    last = GameResult('FAILED', 'PROCESS_EXITED', '未运行')
    attempt = 0
    for attempt in range(1, total + 1):
        if stop_requested():
            last = GameResult('STOPPED', 'STOPPED', '用户停止请求')
            break
        write_status(phase='game', current=game['id'], attempt=attempt)
        update_game_status(game['id'], status='running', tries=attempt,
                           started_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        log.info('===== [%s] 第 %d/%d 次尝试 =====' % (game['name'], attempt, total))
        if game.get('needs_emulator', True) and not ensure_ready(emu):
            last = GameResult('FAILED', 'EMULATOR_NOT_READY',
                              '模拟器启动/ADB连接失败')
        elif not pre_launch_game(game, emu, rot):
            last = GameResult('FAILED', 'GAME_NOT_LAUNCHED',
                              '游戏 APP 未能启动')
        elif game['type'] == 'alas':
            last = run_alas_game(game, emu, attempt, ts)
        elif game['type'] == 'mxu':
            last = run_mxu_game(game, emu, attempt, ts)
        else:
            last = run_marker_game(game, emu, attempt, ts)
        log.info('[%s] 结果: %s [%s] %s'
                 % (game['name'], last.status, last.category, last.detail))
        if last.status in ('SUCCESS', 'SKIPPED') or last.category == 'STOPPED':
            break
        # 维护感知: 日志尾部出现维护提示时, 重试没有意义, 直接跳过
        if last.status == 'FAILED' and last.category != 'EMULATOR_NOT_READY':
            lp = (resolve_log_path(game) if game['type'] == 'marker'
                  else getattr(last, 'sublog', ''))
            if lp and log_mentions_maintenance(lp):
                last = GameResult('SKIPPED', 'MAINTENANCE',
                                  '日志检测到维护/更新提示, 本轮跳过')
                log.warning('[%s] 检测到维护提示, 跳过剩余重试' % game['name'])
                break
        if attempt < total and not stop_requested():
            delay = int(game.get('retry_delay_sec',
                                 rot.get('retry_wait_sec', 10)))
            if delay > 0:
                log.warning('[%s] 冷却 %ds 后重试 (更新/维护等待期)'
                            % (game['name'], delay))
                if not wait_with_stop(delay):
                    last = GameResult('STOPPED', 'STOPPED', '用户停止请求')
                    break
            write_status(phase='recovering')
            do_recovery(game, emu, wait)
    return last, attempt, time.time() - t0


# ---------------- 子命令: run ----------------

def preflight_fatal(cfg):
    errs = []
    ec = cfg['emulator']
    if not os.path.isfile(ec['mumu_manager']):
        errs.append('MuMuManager 不存在: %s' % ec['mumu_manager'])
    if not os.path.isfile(ec['adb']):
        errs.append('adb 不存在: %s' % ec['adb'])
    # 空队列守卫: 没有启用游戏时不起模拟器空转
    enabled_any = any((cfg['games'].get(g) or {}).get('enabled', True)
                      for g in cfg['rotation']['order'])
    if not enabled_any:
        errs.append('轮换队列为空: 先在界面「添加游戏」, 或检查 games/rotation.order')
    for gid in cfg['rotation']['order']:
        g = cfg['games'].get(gid)
        if g is None:
            errs.append('rotation.order 引用了不存在的游戏: %s' % gid)
            continue
        if not g.get('enabled', True):
            continue  # 禁用的游戏不阻塞预检
        # schema 校验 (缺 name/type/必填字段等在这里就拦下, 不带进运行时)
        errs.extend(validate_game(gid, g))
        if g['type'] == 'alas':
            if g.get('mode') == 'ui':
                if not os.path.isfile(g.get('ui_exe', '')):
                    errs.append('%s: ALAS 界面程序不存在: %s'
                                % (gid, g.get('ui_exe', '')))
                if g.get('log_dir') and not os.path.isdir(g['log_dir']):
                    errs.append('%s: ALAS 日志目录不存在: %s'
                                % (gid, g['log_dir']))
            else:
                if not os.path.isfile(g['python']):
                    errs.append('%s: python 不存在: %s' % (gid, g['python']))
                if not os.path.isfile(g['entry']):
                    errs.append('%s: 入口不存在: %s' % (gid, g['entry']))
        elif g['type'] == 'mxu':
            if not os.path.isfile(g['exe']):
                errs.append('%s: exe 不存在: %s' % (gid, g['exe']))
            if g.get('game_task'):
                rc, out, _ = run_cmd(['schtasks', '/query', '/tn',
                                      g['game_task']], timeout=20)
                if rc != 0:
                    errs.append('%s: 计划任务不存在: %s (需先创建 HIGHEST 任务)'
                                % (gid, g['game_task']))
            if g.get('log_dir') and not os.path.isdir(g['log_dir']):
                errs.append('%s: 日志目录不存在: %s' % (gid, g['log_dir']))
        else:
            if not os.path.isfile(g['exe']):
                errs.append('%s: exe 不存在: %s' % (gid, g['exe']))
    # 智能等待探针 (碧蓝等 marker 类游戏若启用则必需)
    if any(cfg['games'][g].get('launch_probe')
           for g in cfg['rotation']['order']
           if g in cfg['games'] and cfg['games'][g].get('enabled', True)):
        for f in ('probe_title.py', 'title_template.png'):
            if not os.path.isfile(os.path.join(BASE_DIR, f)):
                errs.append('智能等待探针缺失: %s' % f)
    return errs


def print_summary(summary, logfile):
    log.info('-' * 60)
    log.info('本轮汇总:')
    ok = 0
    fail = 0
    for name, status, cat, elapsed, detail in summary:
        if status in ('SUCCESS', 'DISABLED'):
            ok += 1
        else:
            fail += 1
        log.info('  %-6s %-9s 耗时 %-9s %s' %
                 (name, status, fmt_elapsed(elapsed),
                  ('[%s] %s' % (cat, detail)) if detail else ''))
    log.info('成功 %d / 失败 %d' % (ok, fail))
    log.info('主日志: %s' % logfile)
    log.info('-' * 60)


# 模拟器静音状态 (异常中止时由 cmd_run 的兜底逻辑恢复原值)
MUTE_STATE = {'manager': '', 'vm': '', 'saved': None}


def _restore_emulator_mute():
    saved = MUTE_STATE.get('saved')
    if saved is None or not MUTE_STATE.get('manager'):
        return
    run_cmd([MUTE_STATE['manager'], 'setting', '-v', MUTE_STATE['vm'],
             '--key', 'system_volume_close', '--value', str(saved)], timeout=15)
    MUTE_STATE['saved'] = None
    log.info('已恢复模拟器声音 (system_volume_close=%s)' % saved)


def cmd_run(cfg):
    """单实例锁 + 轮换主流程"""
    global NOTIFY_CFG
    NOTIFY_CFG = cfg.get('notify', {}) or {}
    ok, holder = acquire_engine_lock()
    if not ok:
        # 不改写 status.json —— 正在运行的引擎才是它的主人
        log.error('已有轮换引擎在运行 (PID %s), 拒绝重复启动。'
                  '如确认无引擎在跑, 删除 engine.lock 后重试'
                  % holder.split(':')[0])
        return 2
    try:
        return _cmd_run_locked(cfg)
    except Exception as e:  # noqa: BLE001
        # 兜底: 不要留下"静音未恢复 + 状态卡在运行中 + 无推送"的半成品现场
        log.error('轮换异常中止: %r' % e)
        try:
            _restore_emulator_mute()
        except Exception:  # noqa: BLE001
            pass
        try:
            # 收敛残留的进行中状态: 否则界面会一直显示"运行中"且停止按钮失效
            for g in STATUS.get('games', []):
                if g.get('status') == 'running':
                    g['status'] = 'FAILED'
                elif g.get('status') == 'pending':
                    g['status'] = 'skipped'
            write_status(phase='error', current='', attempt=0, watch_file='',
                         last_result='轮换异常中止: %s' % (e,))
        except Exception:  # noqa: BLE001
            pass
        try:
            push_notify('轮换异常中止', '%r' % e)
        except Exception:  # noqa: BLE001
            pass
        return 3
    finally:
        release_engine_lock()


def _cmd_run_locked(cfg):
    global NOTIFY_CFG
    NOTIFY_CFG = cfg.get('notify', {}) or {}
    rot = cfg['rotation']
    emu = EmulatorCtl(cfg['emulator'])
    log.info('三游戏轮换启动器 启动 | Python %s' % sys.version.split()[0])

    errs = preflight_fatal(cfg)
    if errs:
        for e in errs:
            log.error('预检失败: %s' % e)
        write_status(phase='error')
        return 2

    if os.path.exists(STOP_FLAG):  # 清掉上次的停止请求
        os.remove(STOP_FLAG)

    # 初始化状态条目
    entries = []
    for gid in rot['order']:
        g = cfg['games'][gid]
        entries.append({'id': gid, 'name': g['name'],
                        'status': 'disabled' if not g.get('enabled', True)
                        else 'pending',
                        'tries': 0, 'elapsed': 0})
    write_status(phase='emulator', current='', attempt=0, watch_file='',
                 games=entries,
                 started_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

    if not ensure_ready(emu):
        log.error('模拟器无法就绪, 中止本轮轮换')
        write_status(phase='error', last_result='模拟器无法就绪')
        return 2

    # 轮换期间静音模拟器 (MuMu 自身的声音开关, 结束后恢复)
    MUTE_STATE.update(manager=emu.manager, vm=emu.vm, saved=None)
    restore_mute = _restore_emulator_mute

    if rot.get('mute_emulator', True):
        rc, out, _ = run_cmd([emu.manager, 'setting', '-v', emu.vm, '--key',
                              'system_volume_close'], timeout=15)
        m = re.search(r'"system_volume_close":\s*"(true|false)"', out)
        saved_mute = m.group(1) if m else None
        MUTE_STATE['saved'] = saved_mute
        run_cmd([emu.manager, 'setting', '-v', emu.vm, '--key',
                 'system_volume_close', '--value', 'true'], timeout=15)
        log.info('已静音模拟器 (原状态 %s)' % (saved_mute or '未知'))

    summary = {}   # gid -> (name, status, cat, elapsed, detail)
    stopped = False

    def run_one(gid):
        game = dict(cfg['games'][gid])
        game['id'] = gid
        if not game.get('enabled', True):
            summary[gid] = (game['name'], 'DISABLED', '', 0, '已在配置中禁用')
            return
        res, tries, elapsed = run_game_with_retry(game, emu, rot)
        update_game_status(gid, status=res.status, tries=tries,
                           elapsed=round(elapsed))
        write_status(current='', attempt=0, watch_file='')
        if rot.get('close_game_after', True):
            emu.app_close(game.get('package', ''))
        advice = ADVICE.get(res.category, '')
        if res.status == 'SUCCESS':
            summary[gid] = (game['name'], 'SUCCESS', '', elapsed, '')
        elif res.status == 'STOPPED':
            summary[gid] = (game['name'], 'STOPPED', '', elapsed,
                            '用户手动停止 | 模拟器保持运行')
        elif res.status == 'SKIPPED':
            summary[gid] = (game['name'], res.status, res.category, elapsed,
                            '%s | 详情: %s' % (res.detail, advice))
        else:
            summary[gid] = (game['name'], res.status, res.category, elapsed,
                            '%s | %s | 子日志: %s'
                            % (res.detail, advice, res.sublog))

    # ---- 通道分发: parallel=true 时 mxu(PC) 走并行 lane, 否则全部串行 ----
    use_parallel = rot.get('parallel', True)
    parallel_gids = []
    serial_gids = []
    for gid in rot['order']:
        g = cfg['games'][gid]
        if not g.get('enabled', True):
            continue
        # 并行通道仅限"无需模拟器"的 PC 端游戏 (如终末地);
        # 需要模拟器的 mxu 游戏 (如 MaaGrandLine) 必须留在串行通道, 避免抢设备
        if use_parallel and g.get('type') == 'mxu' \
                and not g.get('needs_emulator', True):
            parallel_gids.append(gid)
        else:
            serial_gids.append(gid)

    if parallel_gids:
        names = ', '.join(cfg['games'][g]['name'] for g in parallel_gids)
        log.info('并行通道(PC): %s — 与模拟器游戏同时运行' % names)

    def run_pc_lane():
        for gid in parallel_gids:
            if stop_requested():
                summary[gid] = (cfg['games'][gid]['name'],
                                'STOPPED', '', 0, '用户停止请求')
                break
            run_one(gid)

    pc_thread = None
    if parallel_gids:
        pc_thread = threading.Thread(target=run_pc_lane,
                                     name='pc-lane', daemon=True)
        pc_thread.start()

    # ---- 串行 lane: 模拟器游戏 ----
    for gid in serial_gids:
        run_one(gid)
        if summary.get(gid, ('', 'STOPPED'))[1] == 'STOPPED':
            stopped = True
            break
        if stop_requested():
            stopped = True
            break

    # ---- 汇合: 等 PC 通道结束 (用户停止时并行 lane 会在数秒内自行退出) ----
    if pc_thread is not None:
        pc_timeout = sum(int(cfg['games'][g].get('timeout_min', 90))
                         for g in parallel_gids) * 60 + 120
        pc_thread.join(timeout=pc_timeout)
        if pc_thread.is_alive():
            log.warning('并行通道未在预期时间内完成, 继续汇总')

    # 失败复跑(第二遍): 队列跑完后对可重试失败的游戏再试一遍
    # (更新/维护场景: 先跑别的游戏, 冷却期过后回来复跑)
    if rot.get('second_pass', False) and not stopped and not stop_requested():
        failed = [gid for gid in rot['order']
                  if gid in summary and summary[gid][1] == 'FAILED']
        if failed:
            names = ', '.join(cfg['games'][g]['name'] for g in failed)
            log.warning('失败复跑(第二遍): %s' % names)
            write_status(phase='emulator', current='', attempt=0,
                         watch_file='', last_result='')
            for gid in failed:
                if stop_requested():
                    stopped = True
                    break
                run_one(gid)
                if summary.get(gid, ('', 'STOPPED'))[1] == 'STOPPED':
                    stopped = True
                    break
        else:
            log.info('失败复跑: 无可复跑项')

    ordered = [summary[g] for g in rot['order'] if g in summary]

    if stopped:
        for e in STATUS['games']:
            if e['status'] in ('pending', 'running'):
                e['status'] = 'skipped'
        if os.path.exists(STOP_FLAG):
            os.remove(STOP_FLAG)
        write_status(phase='stopped', current='', attempt=0, watch_file='',
                     last_result='已手动停止')
        print_summary(ordered, LOGFILE)
        restore_mute()
        append_history(ordered, os.environ.get('ROTATE_TRIGGER', 'manual'))
        push_notify('轮换被手动停止', last_report(ordered))
        log.info('已停止: 模拟器保持运行, 剩余游戏未执行')
        return 1

    print_summary(ordered, LOGFILE)
    all_ok = all(r[1] in ('SUCCESS', 'DISABLED') for r in ordered)
    ok = sum(1 for r in ordered if r[1] in ('SUCCESS', 'DISABLED'))
    last_result = '成功 %d / 失败 %d' % (ok, len(ordered) - ok)
    restore_mute()
    # 兜底: 界面模式留下的 ALAS 窗口, 在关模拟器前收掉, 否则它会一直截图失败刷屏
    if CURRENT.get('ui_proc') is not None or CURRENT.get('ui_port'):
        log.info('关闭残留的 ALAS 界面进程树...')
        try:
            stop_alas_ui(CURRENT.get('ui_port') or 0)
        except Exception:  # noqa: BLE001
            pass
        CURRENT['ui_proc'] = None
    if rot.get('close_emulator_after', True):
        write_status(phase='shutting_down')
        emu.shutdown(int(cfg['emulator'].get('shutdown_timeout_sec', 90)))
    write_status(phase='finished', last_result=last_result)
    append_history(ordered, os.environ.get('ROTATE_TRIGGER', 'manual'))
    push_notify('游戏轮换' + ('全部成功' if all_ok else '已结束(有异常)'),
                last_result + '\n' + last_report(ordered))
    return 0 if all_ok else 1


def last_report(summary):
    return '\n'.join('%s: %s%s' % (name, status,
                                   (' · ' + detail) if detail else '')
                     for name, status, _c, _e, detail in summary)


# ---------------- 子命令: check ----------------

def cmd_check(cfg):
    return cmd_doctor(cfg, fix=False)


def cmd_doctor(cfg, fix=False):
    """全量体检: 环境/三游戏配置/定时推送; fix=True 自动应用安全修复
    (ALAS 配置可安全修; MAA 配置结构脆弱只给手动指引)"""
    items = []
    fixed = []

    def add(level, name, detail=''):
        items.append((level, name, detail))

    ec = cfg['emulator']
    serial = ec['adb_serial']

    # ---- 1 基础环境 ----
    if os.path.isfile(ec['mumu_manager']):
        rc, out, _ = run_cmd([ec['mumu_manager'], 'version'])
        add('PASS' if rc == 0 else 'FAIL', 'MuMuManager',
            out.strip().replace('\n', ' ') or ec['mumu_manager'])
    else:
        add('FAIL', 'MuMuManager', '不存在: %s' % ec['mumu_manager'])
    add('PASS' if os.path.isfile(ec['adb']) else 'FAIL', 'adb', ec['adb'])
    runtime_py = os.path.join(BASE_DIR, 'runtime', 'python', 'python.exe')
    add('PASS' if os.path.isfile(runtime_py) else 'FAIL', 'runtime.python',
        runtime_py)

    # ---- 2 引擎锁 ----
    if os.path.exists(LOCK_FILE):
        alive = False
        pid = 0
        try:
            pid = int(open(LOCK_FILE).read().strip().split(':')[0])
            alive = _pid_alive(pid)
        except (ValueError, OSError):
            pass
        if alive:
            add('WARN', 'engine.lock', '存在存活引擎 (PID %s), 不能开始新轮换' % pid)
        elif fix:
            try:
                os.remove(LOCK_FILE)
            except OSError:
                pass
            fixed.append('engine.lock')
            add('PASS', 'engine.lock', '陈旧锁已自动清理')
        else:
            add('WARN', 'engine.lock', '陈旧锁残留 (体检修复可自动清理)')
    else:
        add('PASS', 'engine.lock', '无残留')

    # ---- 3 游戏 schema 校验 (遍历全部 games, 不只 order) ----
    for gid, g in cfg.get('games', {}).items():
        if not isinstance(g, dict):
            add('FAIL', gid, '条目不是对象, 无法校验')
            continue
        if not g.get('enabled', True):
            continue
        errs = validate_game(gid, g)
        if errs:
            for e in errs:
                add('FAIL', gid, e)
        else:
            add('PASS', gid, 'schema 校验通过 (%s)' % entry_kind(g))

    # ---- 4 三游戏 ----
    for gid in cfg['rotation']['order']:
        g = cfg['games'].get(gid)
        if g is None:
            add('FAIL', gid, 'rotation.order 引用了不存在的游戏')
            continue
        name = g['name']
        if not g.get('enabled', True):
            add('SKIP', gid, '已在配置中禁用, 跳过检查')
            continue
        if g['type'] == 'alas':
            if g.get('mode') == 'ui':
                add('PASS' if os.path.isfile(g.get('ui_exe', '')) else 'FAIL',
                    '%s.ui_exe' % gid, g.get('ui_exe', ''))
                add('PASS' if os.path.isdir(g.get('log_dir', '')) else 'FAIL',
                    '%s.log_dir' % gid, g.get('log_dir', ''))
            else:
                for k in ('dir', 'python', 'entry'):
                    add('PASS' if os.path.exists(g[k]) else 'FAIL',
                        '%s.%s' % (gid, k), g[k])
            p = os.path.join(g['dir'], 'config', 'alas.json')
            if not os.path.isfile(p):
                add('FAIL', '%s.alas.json' % gid, '不存在: %s' % p)
                continue
            d = jsonload(p)
            # 串口 (可自动修复)
            cur = deep_get(d, 'Alas.Emulator.Serial')
            if cur == serial:
                add('PASS', '%s.Serial' % gid, cur)
            else:
                if fix and os.path.isfile(p):
                    d.setdefault('Alas', {}).setdefault('Emulator', {})[
                        'Serial'] = serial
                    jsonsave(p, d)
                    fixed.append('%s.Serial' % gid)
                    add('PASS', '%s.Serial' % gid, '%s (已自动修复, 原值 %s)'
                        % (serial, cur))
                else:
                    add('FAIL', '%s.Serial' % gid,
                        '为 %s, 应为 %s%s' % (cur, serial,
                                              '' if fix else ' (体检修复可自动修)'))
            # 截图方式 (nemu_ipc 在新版 MuMu 下黑帧, 可自动修复)
            method = deep_get(d, 'Alas.Emulator.ScreenshotMethod')
            if method == 'nemu_ipc':
                if fix:
                    d['Alas']['Emulator']['ScreenshotMethod'] = 'DroidCast'
                    jsonsave(p, d)
                    fixed.append('%s.ScreenshotMethod' % gid)
                    add('PASS', '%s.ScreenshotMethod' % gid,
                        'nemu_ipc -> DroidCast (已自动修复)')
                else:
                    add('WARN', '%s.ScreenshotMethod' % gid,
                        '为 nemu_ipc (新版 MuMu 下会黑帧, 体检修复可改为 DroidCast)')
            else:
                add('PASS', '%s.ScreenshotMethod' % gid, method or '(默认)')
        else:
            add('PASS' if os.path.isfile(g['exe']) else 'FAIL',
                '%s.exe' % gid, g['exe'])

    # ---- 5 火影实例串口 ----
    g = cfg['games'].get('naruto')
    if g is None:
        add('SKIP', 'naruto.instance', '未配置火影, 跳过')
    else:
        app = os.path.join(os.path.dirname(g['exe']), 'appsettings.json')
        if not g.get('enabled', True):
            add('SKIP', 'naruto.instance', '已在配置中禁用, 跳过检查')
        elif os.path.isfile(app):
            try:
                d = jsonload(app)
                inst_id = d.get('Instances.LastActive', '')
                ip = os.path.join(os.path.dirname(g['exe']),
                                  'config', 'instances', inst_id + '.json')
                if inst_id and os.path.isfile(ip):
                    cur = jsonload(ip).get('AdbDevice', {}).get('AdbSerial', '')
                    if cur == serial:
                        add('PASS', 'naruto.instance.AdbSerial', cur)
                    else:
                        add('FAIL', 'naruto.instance.AdbSerial',
                            '为 %s, 应为 %s (在火影界面重新连接一次即可)' % (cur, serial))
                else:
                    add('WARN', 'naruto.instance', '找不到活动实例配置: %s' % ip)
            except Exception as e:  # noqa: BLE001
                add('WARN', 'naruto.instance', '解析失败: %r' % e)
        else:
            add('WARN', 'naruto.appsettings', '不存在: %s' % app)

    # ---- 6 MAA (只读检查 + 手动指引, 不改它的文件) ----
    g = cfg['games'].get('arknights')
    if g is None:
        add('SKIP', 'MAA.config', '未配置明日方舟(MAA), 跳过')
    else:
        p = os.path.join(os.path.dirname(g['exe']), 'config', 'gui.new.json')
        if not os.path.isfile(p):
            p = os.path.join(os.path.dirname(g['exe']), 'config', 'gui.json')
        if not g.get('enabled', True):
            add('SKIP', 'MAA.config', '已在配置中禁用, 跳过检查')
        elif os.path.isfile(p):
            try:
                d = jsonload(p)
                dflt = d.get('Configurations', {}).get('Default', {})
                gui = dflt.get('Gui', {})
                cs = gui.get('ConnectSettings', {})
                su = gui.get('StartUpSettings', {})
                addr = cs.get('Address')
                run_directly = su.get('RunDirectly')
                if addr == serial:
                    add('PASS', 'MAA.Connect.Address', addr)
                else:
                    add('WARN', 'MAA.Connect.Address',
                        '为 %s → MAA设置-连接设置 改为 %s 后点连接' % (addr, serial))
                if run_directly is True:
                    add('PASS', 'MAA.RunDirectly', '启动后直接运行已开启')
                else:
                    add('WARN', 'MAA.RunDirectly',
                        '未开启 → MAA设置-启动设置 勾选「启动后直接运行」')
                client = gui.get('RuntimeSettings', {}).get('ClientType', '')
                if client:
                    add('PASS', 'MAA.ClientType', client)
            except Exception as e:  # noqa: BLE001
                add('WARN', 'MAA.config', '解析失败: %r' % e)
        else:
            add('FAIL', 'MAA.config', '不存在: %s' % p)

    # ---- 6 包名 ----
    for gid in cfg['rotation']['order']:
        g = cfg['games'].get(gid)
        if g is None:
            continue
        if not g.get('enabled', True):
            continue
        if g['type'] == 'mxu' and not g.get('needs_emulator', True):
            add('PASS', '%s.package' % gid, 'PC 端游戏, 无需 Android 包名')
        elif g.get('package'):
            add('PASS', '%s.package' % gid, g['package'])
        else:
            add('WARN', '%s.package' % gid,
                '未配置 (运行 包名识别 或手动填写)')

    # ---- 7 定时与推送 ----
    s = cfg.get('schedule', {})
    if s.get('enabled'):
        if re.match(r'^\d{1,2}:\d{2}$', str(s.get('time', ''))):
            add('PASS', 'schedule', '每日 %s 自动轮换' % s['time'])
        else:
            add('WARN', 'schedule', '时间格式无效: %r' % s.get('time'))
    if (cfg.get('notify', {}) or {}).get('enabled'):
        if (cfg['notify'].get('token') or '').strip():
            add('PASS', 'notify', '已启用 (%s)'
                % cfg['notify'].get('channel', 'serverchan'))
        else:
            add('WARN', 'notify', '已启用但 token 为空, 请在界面填写')

    # ---- 输出 ----
    fails = sum(1 for it in items if it[0] == 'FAIL')
    warns = sum(1 for it in items if it[0] == 'WARN')
    log.info('体检结果%s:' % (' (已应用自动修复)' if fix else ''))
    for level, name, detail in items:
        log.info('  [%s] %-26s %s' % (level, name, detail))
    log.info('FAIL: %d | WARN: %d | 自动修复: %d 项%s'
             % (fails, warns, len(fixed),
                ('(' + ', '.join(fixed) + ')') if fixed else ''))
    if fails or warns:
        log.info('提示: FAIL 多为路径/串口问题(可自动修复); WARN 中 MAA 相关'
                 '需在 MAA 界面手动设置, 其余可运行 体检修复')
    return 1 if fails else 0


def cmd_push_test(cfg):
    global NOTIFY_CFG
    NOTIFY_CFG = cfg.get('notify', {}) or {}
    if not NOTIFY_CFG.get('enabled'):
        log.error('推送未启用, 请先在界面开启并填写 token')
        return 1
    ok = push_notify('轮换控制台测试推送',
                     '收到这条消息说明推送配置成功 ✓\n轮换结束后会把结果推到这里。')
    return 0 if ok else 1


def deep_get(d, path, default=None):
    cur = d
    for k in path.split('.'):
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


# ---------------- 子命令: setup ----------------

def cmd_setup(cfg):
    ec = cfg['emulator']
    serial = ec['adb_serial']
    changed = []

    # ALAS: Serial + EmulatorInfo
    g = cfg['games']['azurlane']
    p = os.path.join(g['dir'], 'config', 'alas.json')
    if os.path.isfile(p):
        backup(p)
        d = jsonload(p)
        alas_sec = d.setdefault('Alas', {})
        alas_sec.setdefault('Emulator', {})['Serial'] = serial
        ei = alas_sec.setdefault('EmulatorInfo', {})
        ei['Emulator'] = 'MuMuPlayer12'
        ei['name'] = 'MuMuPlayer-12.0-%s' % ec.get('vm_index', 0)
        if ec.get('mumu_player_exe'):
            ei['path'] = ec['mumu_player_exe']
        jsonsave(p, d)
        changed.append(p)
        log.info('ALAS 配置已更新: Emulator.Serial=%s, EmulatorInfo.path=%s'
                 % (serial, ei.get('path')))
    else:
        log.error('找不到 ALAS 配置: %s' % p)

    # MAA: 不自动改写其配置 (v6 嵌套结构脆弱, 曾导致 MAA 启动崩溃)
    # 只提示用户在 MAA 界面手动设置一次连接地址 (MAA 自行持久化)
    log.info('MAA 连接配置请在 MAA 界面手动设置一次:')
    log.info('  设置 → 连接设置 → 地址填 %s (adb: %s), 点连接' % (serial, ec['adb']))
    log.info('  MAA 会自行保存, 之后无需再设置')

    log.info('setup 完成, 共修改 %d 个文件 (均有 .bak-* 备份)' % len(changed))
    log.info('建议手动确认:')
    log.info('  1. 打开一次 MAA: 确认能连接 %s, 且日常任务已勾选' % serial)
    log.info('  2. 打开一次火影 MFAAvalonia: 确认任务勾选与连接正常')
    log.info('  3. ALAS 无需打开界面, 轮换器直接以调度器模式运行 alas.py')
    return 0


# ---------------- 子命令: detect-packages ----------------

def cmd_detect_packages(cfg):
    emu = EmulatorCtl(cfg['emulator'])
    if not ensure_ready(emu):
        log.error('模拟器无法就绪, 无法检测包名')
        return 2
    rc, out, err = run_cmd(
        [emu.adb, '-s', emu.serial, 'shell', 'pm', 'list', 'packages', '-3'],
        timeout=30)
    if rc != 0:
        log.error('pm list packages 失败: %r %r' % (out, err))
        return 2
    pkgs = sorted(l.strip()[len('package:'):] for l in out.splitlines()
                  if l.strip().startswith('package:'))
    log.info('共 %d 个第三方包:' % len(pkgs))
    for p in pkgs:
        log.info('  %s' % p)

    cfg_dirty = False
    all_ok = True
    for gid in cfg['rotation']['order']:
        g = cfg['games'].get(gid)
        if g is None:
            log.warning('[%s] rotation.order 引用了不存在的游戏, 跳过' % gid)
            continue
        kws = g.get('package_keywords', [])
        preset = g.get('package', '')
        if preset and preset in pkgs:
            log.info('[%s] 预设包名有效: %s' % (gid, preset))
            continue
        if preset and preset not in pkgs:
            log.warning('[%s] 预设包名未安装: %s, 尝试按关键词重新识别'
                        % (gid, preset))
        matches = [p for p in pkgs
                   if any(k.lower() in p.lower() for k in kws)]
        if len(matches) == 1:
            log.info('[%s] 识别到包名: %s (已写入 config.json)' % (gid, matches[0]))
            g['package'] = matches[0]
            cfg_dirty = True
        elif len(matches) > 1:
            log.warning('[%s] 关键词命中多个, 请手动选择填入 config.json: %s'
                        % (gid, ', '.join(matches)))
            all_ok = False
        else:
            log.warning('[%s] 未按关键词 %s 识别到包名, 请从上面列表手动填写'
                        % (gid, kws))
            all_ok = False
    if cfg_dirty:
        save_config(cfg)
        log.info('config.json 已更新')
    return 0 if all_ok else 1


# ---------------- 子命令: add-game / remove-game / test-game ----------------

def cmd_add_game(cfg, json_path):
    """从 JSON 文件读一个游戏定义并加入轮换 (向导与命令行共用)"""
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            spec = json.load(f)
    except (OSError, ValueError) as e:
        log.error('读取游戏定义失败: %r' % e)
        return 2
    if not isinstance(spec, dict):
        log.error('游戏定义必须是 JSON 对象')
        return 2
    gid = spec.pop('id', None) or spec.pop('gid', None)
    if not gid:
        log.error('游戏定义缺少 id 字段')
        return 2
    ok, errs = add_game(cfg, gid, spec)
    if not ok:
        for e in errs:
            log.error('添加失败: %s' % e)
        return 2
    log.info('已添加游戏: %s (%s)' % (gid, cfg['games'][gid].get('name')))
    log.info('当前轮换顺序: %s'
             % ' → '.join(cfg['rotation'].get('order', [])))
    return 0


def cmd_remove_game(cfg, gid):
    ok, errs = remove_game(cfg, gid)
    if not ok:
        for e in errs:
            log.error('删除失败: %s' % e)
        return 2
    log.info('已删除游戏: %s' % gid)
    log.info('当前轮换顺序: %s'
             % ' → '.join(cfg['rotation'].get('order', [])))
    return 0


def cmd_test_game(cfg, gid):
    """单游戏试跑: 校验 -> 按需起模拟器 -> 跑一遍 -> 收游戏/模拟器"""
    global NOTIFY_CFG
    NOTIFY_CFG = cfg.get('notify', {}) or {}
    ok, holder = acquire_engine_lock()
    if not ok:
        log.error('已有轮换引擎在运行 (PID %s), 无法单测'
                  % holder.split(':')[0])
        return 2
    try:
        game = cfg.get('games', {}).get(gid)
        if game is None:
            log.error('游戏不存在: %s' % gid)
            return 2
        errs = validate_game(gid, game)
        if errs:
            for e in errs:
                log.error('配置校验失败: %s' % e)
            return 2
        if os.path.exists(STOP_FLAG):
            os.remove(STOP_FLAG)
        rot = cfg.get('rotation', {})
        needs_emu = game.get('needs_emulator', True)
        emu = EmulatorCtl(cfg['emulator'])
        started_emu = False
        if needs_emu:
            if not ensure_ready(emu):
                log.error('模拟器无法就绪, 无法单测')
                return 2
            started_emu = True
        g = dict(game)
        g['id'] = gid
        write_status(phase='game', current=gid, attempt=1, watch_file='',
                     games=[{'id': gid, 'name': g['name'], 'status': 'running',
                             'tries': 0, 'elapsed': 0}],
                     started_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        res, tries, elapsed = run_game_with_retry(g, emu, rot)
        update_game_status(gid, status=res.status, tries=tries,
                           elapsed=round(elapsed))
        if rot.get('close_game_after', True):
            emu.app_close(g.get('package', ''))
        # 单测不关模拟器: 方便连续调试/改配置后再测
        write_status(phase='finished', current='', attempt=0, watch_file='',
                     last_result='%s: %s [%s]'
                     % (g['name'], res.status, res.category))
        log.info('单测结果: %s %s [%s] %s'
                 % (g['name'], res.status, res.category, res.detail))
        if started_emu:
            log.info('单测结束: 模拟器保持运行 (正式轮换结束才会关闭)')
        return 0 if res.status in ('SUCCESS', 'SKIPPED') else 1
    except Exception as e:  # noqa: BLE001
        # 兜底: 别留下"脚本进程活着 + 状态卡在运行中"的半成品现场
        log.error('单测异常中止: %r' % e)
        try:
            p = CURRENT.get('proc')
            if p is not None:
                p.kill_tree()
        except Exception:  # noqa: BLE001
            pass
        try:
            for g in STATUS.get('games', []):
                if g.get('status') == 'running':
                    g['status'] = 'FAILED'
                elif g.get('status') == 'pending':
                    g['status'] = 'skipped'
            write_status(phase='error', current='', attempt=0, watch_file='',
                         last_result='单测异常中止: %s' % (e,))
        except Exception:  # noqa: BLE001
            pass
        return 3
    finally:
        release_engine_lock()


# ---------------- 入口 ----------------

def main():
    parser = argparse.ArgumentParser(description='三游戏轮换启动器')
    parser.add_argument('command', nargs='?', default='run',
                        choices=['run', 'check', 'doctor', 'doctor-fix',
                                 'setup', 'detect-packages', 'push-test',
                                 'add-game', 'remove-game', 'test-game'],
                        help='run=完整轮换(默认) doctor=体检 doctor-fix=体检并'
                             '自动修复 setup=首次配置修复 detect-packages='
                             '识别包名 push-test=测试推送 '
                             'add-game=从JSON添加游戏 remove-game=删除游戏 '
                             'test-game=单游戏试跑')
    parser.add_argument('arg', nargs='?', default='',
                        help='add-game: 游戏定义 JSON 路径; '
                             'remove-game/test-game: 游戏 id')
    args = parser.parse_args()

    if args.command in ('add-game', 'remove-game', 'test-game') and not args.arg:
        parser.error('%s 需要一个参数 (add-game: JSON 路径; '
                     'remove-game/test-game: 游戏 id)' % args.command)

    logfile = setup_logger()
    cfg = load_config()
    try:
        if args.command == 'run':
            code = cmd_run(cfg)
        elif args.command == 'check':
            code = cmd_check(cfg)
        elif args.command == 'doctor':
            code = cmd_doctor(cfg, fix=False)
        elif args.command == 'doctor-fix':
            code = cmd_doctor(cfg, fix=True)
        elif args.command == 'setup':
            code = cmd_setup(cfg)
        elif args.command == 'push-test':
            code = cmd_push_test(cfg)
        elif args.command == 'add-game':
            code = cmd_add_game(cfg, args.arg)
        elif args.command == 'remove-game':
            code = cmd_remove_game(cfg, args.arg)
        elif args.command == 'test-game':
            code = cmd_test_game(cfg, args.arg)
        else:
            code = cmd_detect_packages(cfg)
    except KeyboardInterrupt:
        log.warning('用户中断, 结束当前脚本进程...')
        p = CURRENT.get('proc')
        if p is not None:
            p.kill_tree()
        code = 130
    log.info('主日志: %s' % logfile)
    return code


if __name__ == '__main__':
    sys.exit(main())

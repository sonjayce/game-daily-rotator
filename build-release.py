#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build-release.py — 组装 GitHub Release 压缩包
=============================================
前置: 已执行 `cd electron && npm run dist` (产物在 electron/dist-out/win-unpacked/)

把以下内容打进一个 zip (解压即用, 无需再配置):
  - win-unpacked/            界面本体 (exe + Electron 运行时 + resources/app.asar)
  - runtime/python/          嵌入式 Python 引擎运行时
  - rotate.py / probe_title.py / start_rotation.bat   引擎与命令行入口
  - config.example.json      配置模板 (首次运行也会自动生成骨架)
  - README.md                使用说明

输出: release/game-daily-rotator-v<版本>-win-x64.zip
用法: python build-release.py
"""
import json
import os
import sys
import zipfile
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
UNPACKED = os.path.join(BASE, 'electron', 'dist-out', 'win-unpacked')
RUNTIME = os.path.join(BASE, 'runtime', 'python')
RELEASE_DIR = os.path.join(BASE, 'release')
EXTRA_FILES = ['rotate.py', 'probe_title.py', 'start_rotation.bat',
               'config.example.json', 'README.md']


def fail(msg):
    print('[ERROR] %s' % msg)
    sys.exit(1)


def add_tree(zf, src_dir, prefix):
    n = 0
    for root, _dirs, files in os.walk(src_dir):
        for f in files:
            full = os.path.join(root, f)
            rel = os.path.relpath(full, src_dir).replace('\\', '/')
            zf.write(full, '%s/%s' % (prefix, rel))
            n += 1
    return n


def main():
    if not os.path.isdir(UNPACKED):
        fail('未找到构建产物: %s\n请先: cd electron && npm install && npm run dist'
             % UNPACKED)
    if not os.path.isfile(os.path.join(RUNTIME, 'python.exe')):
        fail('未找到嵌入式 Python: %s\n(可从 python.org 下载 embeddable zip '
             '解压到 runtime/python/, 或运行 setup-runtime.bat)' % RUNTIME)

    ver = json.load(open(os.path.join(BASE, 'electron', 'package.json'),
                         encoding='utf-8')).get('version', '0.0.0')
    top = '游戏轮换控制台-v%s' % ver
    os.makedirs(RELEASE_DIR, exist_ok=True)
    out = os.path.join(RELEASE_DIR,
                       'game-daily-rotator-v%s-win-x64.zip' % ver)

    n_all = 0
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        n_all += add_tree(zf, UNPACKED, top)
        print('  界面本体: %d 个文件' % n_all)
        n_rt = add_tree(zf, RUNTIME, top + '/runtime/python')
        n_all += n_rt
        print('  Python 运行时: %d 个文件' % n_rt)
        for f in EXTRA_FILES:
            src = os.path.join(BASE, f)
            if os.path.isfile(src):
                zf.write(src, '%s/%s' % (top, f))
                n_all += 1
            else:
                print('  [WARN] 跳过缺失文件: %s' % f)

    size_mb = os.path.getsize(out) / 1024 / 1024
    print('\n完成: %s' % out)
    print('  版本 %s | %d 个文件 | %.1f MB | %s'
          % (ver, n_all, size_mb, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    print('上传: GitHub → Releases → Draft a new release → 拖入此 zip')


if __name__ == '__main__':
    main()

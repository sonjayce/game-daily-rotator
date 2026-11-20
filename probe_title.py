#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""碧蓝航线标题页探测: 截屏 + 模板匹配
用法: python probe_title.py <adb路径> <serial> <模板路径>
输出: TITLE (标题页出现) / NO (任何失败都视为 NO, 探测是尽力而为)
需在带 cv2+numpy 的 Python 下运行 (ALAS toolkit)
"""
import os
import subprocess
import sys
import tempfile

import cv2
import numpy as np

THRESHOLD = 0.72
TMP = tempfile.gettempdir()


def imfrombytes(data):
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)


def cap_exec_out(adb, serial):
    p = subprocess.run([adb, '-s', serial, 'exec-out', 'screencap'],
                       capture_output=True, timeout=20)
    return imfrombytes(p.stdout)


def cap_two_step(adb, serial):
    remote = '/sdcard/rot_probe.png'
    subprocess.run([adb, '-s', serial, 'shell', 'screencap', '-p', remote],
                   capture_output=True, timeout=20)
    local = os.path.join(TMP, 'rot_probe.png')
    subprocess.run([adb, '-s', serial, 'pull', remote, local],
                   capture_output=True, timeout=20)
    subprocess.run([adb, '-s', serial, 'shell', 'rm', remote],
                   capture_output=True, timeout=15)
    try:
        return cv2.imdecode(np.fromfile(local, dtype=np.uint8),
                            cv2.IMREAD_COLOR)
    except Exception:  # noqa: BLE001
        return cv2.imread(local)


def cap(adb, serial):
    try:
        img = cap_exec_out(adb, serial)
        if img is not None:
            return img
    except Exception:  # noqa: BLE001
        pass
    try:
        return cap_two_step(adb, serial)
    except Exception:  # noqa: BLE001
        return None


def main():
    adb, serial, tpl_path = sys.argv[1], sys.argv[2], sys.argv[3]
    try:  # cv2.imread 不支持中文路径
        tpl = cv2.imdecode(np.fromfile(tpl_path, dtype=np.uint8),
                           cv2.IMREAD_COLOR)
    except Exception:  # noqa: BLE001
        tpl = cv2.imread(tpl_path)
    img = cap(adb, serial)
    if tpl is None or img is None:
        print('NO')
        return
    score = float(cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED).max())
    sys.stderr.write('title_match=%.3f\n' % score)
    print('TITLE' if score >= THRESHOLD else 'NO')


if __name__ == '__main__':
    main()

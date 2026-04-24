#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
模拟鼠标点击，防止电脑熄屏
按 Ctrl+C 停止
"""

import ctypes
import ctypes.wintypes
import time

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000

user32 = ctypes.windll.user32


def move_mouse():
    """微微移动鼠标再移回，防止熄屏"""
    pt = ctypes.wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    x, y = pt.x, pt.y
    user32.SetCursorPos(x + 1, y)
    time.sleep(0.05)
    user32.SetCursorPos(x, y)


if __name__ == '__main__':
    print("防熄屏脚本已启动，每30秒模拟一次鼠标移动")
    print("按 Ctrl+C 停止")
    try:
        while True:
            move_mouse()
            time.sleep(30)
    except KeyboardInterrupt:
        print("\n已停止")

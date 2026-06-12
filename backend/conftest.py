"""pytest 引导：确保 backend 目录在 import 路径上，使 ``import app...`` 可用。

测试从 backend 目录运行（见根 Makefile 的 ``test`` 目标）。该目录即包根，
显式加入 sys.path[0] 避免依赖运行方式的隐式路径推断。
"""

import os
import sys

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

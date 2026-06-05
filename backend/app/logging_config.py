"""日志配置模块

按时间分目录的文件日志：
  logs/{YYYY-MM-DD}/{service_name}/{HH}.log

目录结构：
  logs/
  ├── 2026-06-06/
  │   ├── backend/
  │   │   ├── 00.log
  │   │   ├── 01.log
  │   │   └── ...
  │   └── worker/
  │       └── ...
  └── 2026-06-07/
      └── ...

日志保留 15 天，过期目录由后台清理。
同时保留 stdout 输出（docker logs 仍然可用）。
"""

import logging
import os
import shutil
import threading
import time
from datetime import datetime, timedelta
from logging.handlers import BaseRotatingHandler
from pathlib import Path

# 日志保留天数
LOG_RETENTION_DAYS = 15

# 日志根目录（相对于工作目录，容器内映射为 /app/logs）
LOG_BASE_DIR = Path(os.environ.get("LOG_DIR", "logs"))


class HourlyDirectoryHandler(BaseRotatingHandler):
    """按小时切分日志到目录结构的 Handler

    输出路径: {base_dir}/{YYYY-MM-DD}/{service_name}/{HH}.log
    每小时自动切换文件，无需 rotate 机制（直接换文件路径）。
    """

    def __init__(self, base_dir: Path, service_name: str, encoding: str = "utf-8"):
        self.base_dir = base_dir
        self.service_name = service_name
        self._current_hour: str = ""
        self._current_date: str = ""

        # 初始化时确定当前文件路径
        filepath = self._get_current_filepath()
        super().__init__(str(filepath), mode="a", encoding=encoding)

    def _get_current_filepath(self) -> Path:
        """根据当前时间计算日志文件路径"""
        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        hour_str = now.strftime("%H")

        self._current_date = date_str
        self._current_hour = hour_str

        dir_path = self.base_dir / date_str / self.service_name
        dir_path.mkdir(parents=True, exist_ok=True)
        return dir_path / f"{hour_str}.log"

    def shouldRollover(self, record) -> int:
        """检查是否需要切换文件（跨小时）"""
        now = datetime.now()
        current_hour = now.strftime("%H")
        current_date = now.strftime("%Y-%m-%d")

        if current_hour != self._current_hour or current_date != self._current_date:
            return 1
        return 0

    def doRollover(self):
        """切换到新的小时日志文件"""
        if self.stream:
            self.stream.close()
            self.stream = None

        filepath = self._get_current_filepath()
        self.baseFilename = str(filepath)
        self.stream = self._open()


def _cleanup_old_logs(base_dir: Path, retention_days: int) -> None:
    """清理过期日志目录（删除 retention_days 天前的日期目录）"""
    if not base_dir.exists():
        return

    cutoff_date = datetime.now() - timedelta(days=retention_days)
    cutoff_str = cutoff_date.strftime("%Y-%m-%d")

    for item in base_dir.iterdir():
        if item.is_dir() and len(item.name) == 10:  # YYYY-MM-DD 格式
            try:
                if item.name < cutoff_str:
                    shutil.rmtree(item)
                    logging.getLogger(__name__).info("清理过期日志目录: %s", item.name)
            except (ValueError, OSError) as e:
                logging.getLogger(__name__).debug("清理日志目录失败 %s: %s", item.name, e)


def _start_cleanup_timer(base_dir: Path, retention_days: int) -> None:
    """启动后台线程，每小时检查一次过期日志"""

    def _cleanup_loop():
        while True:
            try:
                _cleanup_old_logs(base_dir, retention_days)
            except Exception:
                pass
            time.sleep(3600)  # 每小时检查一次

    t = threading.Thread(target=_cleanup_loop, daemon=True, name="log-cleanup")
    t.start()


def setup_logging(service_name: str, level: int = logging.INFO) -> None:
    """配置应用日志

    Args:
        service_name: 服务标识（backend / worker），用作日志子目录名
        level: 日志级别
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # 日志格式
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 1. 控制台输出（保留 docker logs 可用）
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(level)

    # 2. 文件输出（按小时切分到目录）
    file_handler = HourlyDirectoryHandler(
        base_dir=LOG_BASE_DIR,
        service_name=service_name,
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)

    # 清除已有 handler（避免重复添加）
    root_logger.handlers.clear()
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    # 降低第三方库日志级别
    for noisy_logger in ("httpx", "httpcore", "uvicorn.access", "asyncio"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    # 启动日志清理定时任务
    _start_cleanup_timer(LOG_BASE_DIR, LOG_RETENTION_DAYS)

    logging.getLogger(__name__).info(
        "日志已配置: service=%s, dir=%s, retention=%d天",
        service_name, LOG_BASE_DIR, LOG_RETENTION_DAYS,
    )

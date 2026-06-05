"""运行内存检测与单库 chunk 上限（KB_Chunk_Cap）推荐（design C3）

本模块为「超级管理员配置单库 chunk 硬上限」提供一个基于运行内存的**信息性推荐值**
（Req 5）：

- ``detect_memory_limit_bytes``：探测当前运行内存上限。优先读取容器内存限制
  （cgroup v2 ``memory.max`` → 回退 cgroup v1 ``memory.limit_in_bytes``），二者均不可用
  或未设限时回退物理内存（``psutil.virtual_memory().total``）。
- ``recommend_kb_chunk_cap``：由「可用内存预算（检测内存 × 安全系数）÷ 单 chunk 内存占用
  ÷ 预计同时活跃库数」反推一个**偏保守**的推荐 chunk 上限，连同检测内存与假设说明一并返回。

设计约束（对照 requirements Req 5.2 / 5.3 / 5.5 与 design「Correctness Properties · Property 6」）：

- 推荐值随检测内存单调不减、随活跃库数单调不增，且**不超过** ``内存 × 安全系数 ÷ 单chunk ÷ 活跃库数``
  （保守、不超卖）。
- 仅作信息性建议，**不自动写入**生效配置（写库由超管确认后经 PlatformConfigStore 完成）。
- 内存检测失败或读到异常值时**安全降级**（不抛错、不阻塞配置页），以保守默认推荐值兜底。

``psutil`` 采用**惰性导入**：未安装或导入失败时不影响本模块加载，仅使物理内存回退路径降级。
"""

import logging
import math

logger = logging.getLogger(__name__)


# ============================================================
# 模块常量（单一事实源，避免魔法值）
# ============================================================

# 单 child chunk 常驻内存估算（dense 向量 + hnsw 图 + sparse + bm25 + content + 标量）
_CHUNK_BYTES = 8 * 1024
# 向量库可用内存占总内存比例（其余留给 OS / 模型 / PG / Redis 等）
_SAFETY_FACTOR = 0.35
# 预计同时活跃知识库数（保守假设）
_DEFAULT_ACTIVE_KBS = 2

# 推荐值向下取整粒度（chunk）
_ROUND_GRANULARITY = 1_000
# KB_Chunk_Cap 合法范围（对齐 design C1 / requirements 配置汇总表）
_KB_CHUNK_CAP_MIN = 10_000
_KB_CHUNK_CAP_MAX = 10_000_000
# 检测失败 / 异常值时兜底的保守默认推荐
_CONSERVATIVE_DEFAULT_CAP = 100_000

_GIB = 1024 ** 3

# cgroup 内存限制文件路径
_CGROUP_V2_MAX = "/sys/fs/cgroup/memory.max"
_CGROUP_V1_LIMIT = "/sys/fs/cgroup/memory/memory.limit_in_bytes"
# cgroup「未设限」哨兵阈值：超过该值视为未限制（v1 常见哨兵 ~9.2e18，v2 为字面量 "max"）
_CGROUP_UNLIMITED_THRESHOLD = 1 << 62


# ============================================================
# 内存检测（cgroup v2 → cgroup v1 → psutil 物理内存）
# ============================================================

def _read_cgroup_v2() -> int | None:
    """读取 cgroup v2 ``memory.max``。

    Returns:
        有效的内存上限字节数；文件不存在、内容为 ``"max"``（未设限）、
        值越界或解析失败时返回 ``None``（交由上层回退）。
    """
    try:
        with open(_CGROUP_V2_MAX, "r", encoding="utf-8") as f:
            raw = f.read().strip()
    except OSError:
        return None
    if not raw or raw == "max":
        return None
    try:
        val = int(raw)
    except ValueError:
        return None
    if 0 < val < _CGROUP_UNLIMITED_THRESHOLD:
        return val
    return None


def _read_cgroup_v1() -> int | None:
    """读取 cgroup v1 ``memory.limit_in_bytes``。

    Returns:
        有效的内存上限字节数；文件不存在、值达到「未设限」哨兵阈值或解析失败时返回 ``None``。
    """
    try:
        with open(_CGROUP_V1_LIMIT, "r", encoding="utf-8") as f:
            raw = f.read().strip()
    except OSError:
        return None
    try:
        val = int(raw)
    except ValueError:
        return None
    if 0 < val < _CGROUP_UNLIMITED_THRESHOLD:
        return val
    return None


def detect_memory_limit_bytes() -> int:
    """探测当前运行内存上限（字节）。

    探测顺序（Req 5.2）：

    1. 容器内存限制 cgroup v2 ``memory.max``；
    2. 回退 cgroup v1 ``memory.limit_in_bytes``；
    3. 二者均不可用或未设限 → 回退物理内存 ``psutil.virtual_memory().total``。

    本函数不抛出异常：所有检测路径均不可用时返回 ``0``，由 :func:`recommend_kb_chunk_cap`
    据此安全降级为保守默认推荐（Req 5.5）。

    Returns:
        检测到的内存上限字节数；全部失败时返回 ``0``。
    """
    v2 = _read_cgroup_v2()
    if v2 is not None:
        return v2

    v1 = _read_cgroup_v1()
    if v1 is not None:
        return v1

    # 物理内存回退：psutil 惰性导入，未安装/异常时降级
    try:
        import psutil

        total = int(psutil.virtual_memory().total)
        if total > 0:
            return total
    except Exception:  # noqa: BLE001 - 检测路径必须安全降级，不向上抛
        logger.warning("psutil 物理内存检测不可用，内存检测降级", exc_info=True)

    return 0


# ============================================================
# 单库 chunk 上限推荐（纯函数核心 + 对外接口）
# ============================================================

def _recommended_cap_for(detected_bytes: float | int | None, active_kbs: int | None) -> int:
    """由检测内存与活跃库数反推保守推荐 chunk 上限（纯函数，便于属性测试）。

    公式：``推荐 = floor(detected_bytes × _SAFETY_FACTOR ÷ _CHUNK_BYTES ÷ active_kbs)``，
    再向下取整到 :data:`_ROUND_GRANULARITY` 粒度，并以 :data:`_KB_CHUNK_CAP_MAX` 封顶。

    性质（Property 6）：随 ``detected_bytes`` 单调不减、随 ``active_kbs`` 单调不增，
    且恒 ``<= detected_bytes × _SAFETY_FACTOR ÷ _CHUNK_BYTES ÷ active_kbs``（保守、不超卖）。

    Args:
        detected_bytes: 检测内存字节数；非正数 / 非有限值 / ``None`` 视为异常。
        active_kbs: 预计同时活跃库数；``< 1`` 或非整数视为异常。

    Returns:
        推荐 chunk 上限；输入异常时返回 :data:`_CONSERVATIVE_DEFAULT_CAP`（不抛错）。
    """
    try:
        if detected_bytes is None or active_kbs is None:
            return _CONSERVATIVE_DEFAULT_CAP
        kbs = int(active_kbs)
        detected = float(detected_bytes)
        if kbs < 1 or not math.isfinite(detected) or detected <= 0:
            return _CONSERVATIVE_DEFAULT_CAP

        raw = detected * _SAFETY_FACTOR / _CHUNK_BYTES / kbs
        cap = math.floor(raw)
        # 向下取整到粒度（保持 <= raw 与单调性）
        cap -= cap % _ROUND_GRANULARITY
        # 上界封顶（MAX 为粒度整数倍，不破坏 <= raw 与单调性）
        cap = min(cap, _KB_CHUNK_CAP_MAX)
        if cap < 0:
            cap = 0
        return cap
    except Exception:  # noqa: BLE001 - 推荐计算必须安全降级
        logger.warning("推荐值计算异常，使用保守默认", exc_info=True)
        return _CONSERVATIVE_DEFAULT_CAP


def recommend_kb_chunk_cap(active_kbs: int = _DEFAULT_ACTIVE_KBS) -> dict:
    """基于运行内存反推单库 chunk 上限的保守推荐值（Req 5.1 / 5.3 / 5.6）。

    仅作信息性建议，**不自动写入**生效配置（Req 5.4）。内存检测失败或读到异常值时
    安全降级为保守默认推荐，不抛错、不阻塞配置页（Req 5.5）。

    Args:
        active_kbs: 预计同时活跃的知识库数（保守假设），默认 :data:`_DEFAULT_ACTIVE_KBS`。

    Returns:
        dict，含：

        - ``detected_memory_gb`` (float): 检测内存（GiB，保留两位；检测失败为 ``0.0``）。
        - ``recommended_kb_chunk_cap`` (int): 推荐 chunk 上限。
        - ``safety_factor`` (float): 计算所用安全系数。
        - ``active_kbs_assumption`` (int): 计算所用活跃库数假设。
        - ``assumption`` (str): 假设说明文案，供超管据此手动调整。
    """
    try:
        detected_bytes = detect_memory_limit_bytes()
    except Exception:  # noqa: BLE001 - detect 本身已安全降级，这里再兜一层
        logger.warning("内存检测失败，使用保守默认推荐", exc_info=True)
        detected_bytes = 0

    cap = _recommended_cap_for(detected_bytes, active_kbs)

    detected_gb = round(detected_bytes / _GIB, 2) if detected_bytes and detected_bytes > 0 else 0.0

    try:
        kbs_assumption = int(active_kbs)
        if kbs_assumption < 1:
            kbs_assumption = _DEFAULT_ACTIVE_KBS
    except (TypeError, ValueError):
        kbs_assumption = _DEFAULT_ACTIVE_KBS

    assumption = (
        f"按同时活跃 {kbs_assumption} 个知识库、安全系数 {_SAFETY_FACTOR}、"
        f"单 chunk 约 {_CHUNK_BYTES // 1024}KB 常驻内存估算（偏保守，仅供参考，不自动生效）"
    )

    return {
        "detected_memory_gb": detected_gb,
        "recommended_kb_chunk_cap": cap,
        "safety_factor": _SAFETY_FACTOR,
        "active_kbs_assumption": kbs_assumption,
        "assumption": assumption,
    }

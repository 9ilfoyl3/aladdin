"""实测 ``collection.load()`` 开销（轻量版 B3 前置门禁）—— 一次性测量工具，非断言式测试。

设计依据：design.md Components C6.3（前置实测）、requirements Req 13.1 / 13.2 / 13.3。

轻量版 B3 的目标是"跳过对已加载 collection 的重复 ``collection.load()``"。是否值得落地
（实现 Req 14 / 15）取决于 ``collection.load()`` 在**已加载**库上的真实开销。本脚本对一个
目标知识库的 collection 连续多次调用 ``collection.load()``，统计耗时分布，给出是否采纳的
判定建议，作为 13.2 / 13.3 实现的**前置门禁**：

- 中位数 > 50ms  → 建议采纳轻量版 B3（实现 Req 14 / 15）。
- 中位数 < 5ms   → 不建议采纳（重复 load 几乎无开销）。
- 5ms ~ 50ms     → 灰色区间，由实测数据与收益权衡决定。

判定阈值来自 requirements Req 13.2 / 13.3。

放在 ``app/scripts/`` 而非 ``tests/``：它是一次性测量工具，输出供人观察，不做断言。
**必须在能连到真实 Milvus、且目标知识库已入库（collection 已存在且可加载）的环境运行**；
在没有真实 Milvus 的环境只应做导入校验，不应实际执行测量。

脚本为**同步**实现：pymilvus 是同步 API，直接调用并用 ``time.perf_counter`` 计时即可，
无需 asyncio。

用法示例（在 ``aladdin/backend`` 目录，aladdin conda 环境）::

    conda run -n aladdin python -m app.scripts.measure_load_overhead --kb-id <KB_ID>
    conda run -n aladdin python -m app.scripts.measure_load_overhead --kb-id <KB_ID> -n 50
"""

from __future__ import annotations

import argparse
import statistics
from dataclasses import dataclass, field
from time import perf_counter

# 判定阈值（毫秒）—— 来自 requirements Req 13.2 / 13.3，禁止散落魔法值。
_ADOPT_THRESHOLD_MS = 50.0  # 中位数 > 此值 → 建议采纳轻量版 B3
_REJECT_THRESHOLD_MS = 5.0  # 中位数 < 此值 → 不建议采纳
# 默认测量迭代次数（含首次冷调用，统计时丢弃首次）。
_DEFAULT_ITERATIONS = 20

# 判定建议文案常量。
_RECOMMEND_ADOPT = "建议采纳轻量版 B3（实现 Req 14/15）"
_RECOMMEND_REJECT = "不建议采纳轻量版 B3"
_RECOMMEND_GRAY = "灰色区间，由实测数据与收益权衡决定"


@dataclass
class LoadOverheadResult:
    """``collection.load()`` 开销测量结果。

    Attributes:
        kb_id: 目标知识库 id。
        iterations: 实际测量迭代次数（含被丢弃的首次冷调用）。
        samples: 每次 ``collection.load()`` 的耗时（毫秒），按调用顺序，含首次冷调用。
        median: 丢弃首次后剩余样本的中位数（毫秒），即 Load_Overhead。
        min: 丢弃首次后剩余样本的最小值（毫秒）。
        max: 丢弃首次后剩余样本的最大值（毫秒）。
        mean: 丢弃首次后剩余样本的均值（毫秒）。
        recommendation: 基于中位数与阈值得出的判定建议。
    """

    kb_id: str
    iterations: int
    samples: list[float] = field(default_factory=list)
    median: float = 0.0
    min: float = 0.0
    max: float = 0.0
    mean: float = 0.0
    recommendation: str = ""


def _recommendation_for(median_ms: float) -> str:
    """根据中位数（毫秒）给出判定建议（阈值见 Req 13.2 / 13.3）。"""
    if median_ms > _ADOPT_THRESHOLD_MS:
        return _RECOMMEND_ADOPT
    if median_ms < _REJECT_THRESHOLD_MS:
        return _RECOMMEND_REJECT
    return _RECOMMEND_GRAY


def measure_load_overhead(kb_id: str, n: int = _DEFAULT_ITERATIONS) -> LoadOverheadResult:
    """实测对已加载 collection 重复调用 ``collection.load()`` 的开销。

    流程：
    1. 用 ``get_settings()`` 的 host/port 构造 ``MilvusClient`` 并 ``_connect()``。
    2. 取目标 collection（``client._collection_name(kb_id)``）。
    3. 先调用一次 ``collection.load()`` 作为预热，确保进入 loaded 态。
    4. 循环 N 次测量每次 ``collection.load()`` 的耗时（毫秒）。
    5. **丢弃第 1 次**（冷调用），取后续 (N-1) 次的中位数作为 Load_Overhead。
    6. 同时给出 min / median / max / mean，并据中位数得出判定建议。

    Args:
        kb_id: 目标知识库 id（其 collection 须已存在并可加载）。
        n: 测量迭代次数，默认 20（含首次冷调用，统计时丢弃首次）。

    Returns:
        ``LoadOverheadResult``，含 samples / median / min / max / mean / recommendation。

    Raises:
        ValueError: ``n`` < 2（无法在丢弃首次后保留样本）。
    """
    if n < 2:
        raise ValueError(f"迭代次数 n 必须 >= 2（丢弃首次后需保留样本），当前 n={n}")

    # 延迟导入：pymilvus / 配置仅在实际测量时需要，导入本模块（如语法校验）不应强依赖。
    from pymilvus import Collection

    from app.config import get_settings
    from app.storage.milvus import MilvusClient

    settings = get_settings()
    client = MilvusClient(host=settings.milvus_host, port=settings.milvus_port)
    client._connect()

    name = client._collection_name(kb_id)
    collection = Collection(name=name, using=client._alias)

    # 预热：确保 collection 处于 loaded 态，使后续测量反映"已加载库重复 load"的真实开销。
    collection.load()

    samples: list[float] = []
    for _ in range(n):
        t0 = perf_counter()
        collection.load()
        t1 = perf_counter()
        samples.append((t1 - t0) * 1000.0)

    # 丢弃第 1 次（冷调用），用剩余样本统计 Load_Overhead。
    measured = samples[1:]
    median_ms = statistics.median(measured)

    return LoadOverheadResult(
        kb_id=kb_id,
        iterations=n,
        samples=samples,
        median=median_ms,
        min=min(measured),
        max=max(measured),
        mean=statistics.mean(measured),
        recommendation=_recommendation_for(median_ms),
    )


def print_result(result: LoadOverheadResult) -> None:
    """终端打印测量结果，突出中位数（Load_Overhead）与判定建议。"""
    print(f"\n=== collection.load() 开销实测 kb_id={result.kb_id} ===")
    print(f"迭代次数: {result.iterations}（丢弃首次冷调用，统计 {result.iterations - 1} 次）")
    if result.samples:
        print(f"首次冷调用: {result.samples[0]:.3f} ms（已丢弃）")
    print("-" * 48)
    print(f"  min   : {result.min:.3f} ms")
    print(f"  median: {result.median:.3f} ms  <<< Load_Overhead")
    print(f"  max   : {result.max:.3f} ms")
    print(f"  mean  : {result.mean:.3f} ms")
    print("-" * 48)
    print(
        f"判定阈值: 采纳 > {_ADOPT_THRESHOLD_MS:.0f}ms / 不采纳 < {_REJECT_THRESHOLD_MS:.0f}ms "
        f"（来自 Req 13.2/13.3）"
    )
    print(f">>> 判定建议: {result.recommendation}")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "实测 collection.load() 在已加载库上的开销，作为轻量版 B3 是否落地的前置门禁。"
        ),
    )
    parser.add_argument("--kb-id", required=True, help="目标知识库 id（collection 须已存在并可加载）")
    parser.add_argument(
        "-n",
        "--iterations",
        type=int,
        default=_DEFAULT_ITERATIONS,
        help=f"测量迭代次数（含首次冷调用，统计时丢弃首次），默认 {_DEFAULT_ITERATIONS}",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """CLI 入口：实测并打印结果。"""
    args = _parse_args(argv)
    result = measure_load_overhead(args.kb_id, n=args.iterations)
    print_result(result)


if __name__ == "__main__":
    main()

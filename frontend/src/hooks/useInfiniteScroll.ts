import { useEffect, useRef } from 'react'

/**
 * 滚动加载哨兵 Hook。
 *
 * 将返回的 ref 绑定到列表底部的占位元素，当该元素进入视口时自动触发 onLoadMore。
 * 配合 react-query 的 useInfiniteQuery 使用。
 *
 * @param onLoadMore 触底时的加载回调（通常是 fetchNextPage）
 * @param options.hasMore 是否还有下一页
 * @param options.loading 是否正在加载（防止重复触发）
 * @param options.root 滚动容器，为空时使用视口
 * @param options.rootMargin 提前触发的边距，默认提前 200px 预加载
 */
export function useInfiniteScroll(
  onLoadMore: () => void,
  options: {
    hasMore: boolean
    loading: boolean
    root?: Element | null
    rootMargin?: string
  }
) {
  const { hasMore, loading, root = null, rootMargin = '200px' } = options
  const sentinelRef = useRef<HTMLDivElement | null>(null)
  // 用 ref 持有最新回调，避免 observer 频繁重建
  const onLoadMoreRef = useRef(onLoadMore)
  onLoadMoreRef.current = onLoadMore

  useEffect(() => {
    const el = sentinelRef.current
    if (!el || !hasMore) return

    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting && !loading) {
          onLoadMoreRef.current()
        }
      },
      { root, rootMargin }
    )
    observer.observe(el)
    return () => observer.disconnect()
  }, [hasMore, loading, root, rootMargin])

  return sentinelRef
}

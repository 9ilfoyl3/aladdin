// 设备性能检测与运行时降级工具
//
// 用于决定是否渲染重度 WebGL 动效（如登录页的 Prism 背景）。
// 分两层：
//   1. detectLowEndDevice() —— 进页面前的静态预判（CPU/内存/动效偏好/软件渲染等）
//   2. useAdaptivePerf()    —— 渲染期实时采样 FPS，持续过低则自动降级
//
// 设计原则：宁可保守降级，也不要让低端机卡顿。检测一旦判定低端，不再回升。

import { useEffect, useRef, useState } from 'react'

// 低端判定阈值
const MIN_CPU_CORES = 4 // 逻辑核心数低于此值视为低端
const MIN_DEVICE_MEMORY_GB = 4 // 内存（GB）低于此值视为低端

// 运行时 FPS 兜底阈值
const FPS_SAMPLE_MS = 2500 // 采样窗口
const FPS_DOWNGRADE_THRESHOLD = 30 // 平均 FPS 低于此值则降级

let cachedLowEnd: boolean | null = null

/**
 * 读取部署人员在 public/config.js 中设置的动态背景策略。
 * 读不到（文件缺失或字段未填）时默认 'auto'。
 */
function getPrismMode(): PrismBackgroundMode {
  if (typeof window === 'undefined') return 'auto'
  const mode = window.__APP_CONFIG__?.prismBackground
  return mode === 'on' || mode === 'off' ? mode : 'auto'
}

/**
 * 检测 WebGL 是否运行在软件渲染器上（如 SwiftShader / llvmpipe / Microsoft Basic Render）。
 * 软件渲染意味着没有可用 GPU，跑 shader 会极慢。
 */
function isSoftwareWebGL(): boolean {
  try {
    const canvas = document.createElement('canvas')
    const gl = (canvas.getContext('webgl') ||
      canvas.getContext('experimental-webgl')) as WebGLRenderingContext | null
    if (!gl) return true // 拿不到 WebGL 上下文，直接当低端

    const ext = gl.getExtension('WEBGL_debug_renderer_info')
    if (!ext) return false // 拿不到 renderer 信息，无法判定，按非软件处理
    const renderer = String(
      gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) || '',
    ).toLowerCase()
    return (
      renderer.includes('swiftshader') ||
      renderer.includes('llvmpipe') ||
      renderer.includes('software') ||
      renderer.includes('basic render')
    )
  } catch {
    return true
  }
}

/**
 * 静态预判设备是否为低端，需要关闭重度动效。结果缓存，整个会话只算一次。
 */
export function detectLowEndDevice(): boolean {
  if (cachedLowEnd !== null) return cachedLowEnd
  if (typeof window === 'undefined') return false

  // 部署人员强制指定：off 视为低端（强制降级），on 视为高端（强制开启）
  const mode = getPrismMode()
  if (mode === 'off') {
    cachedLowEnd = true
    return cachedLowEnd
  }
  if (mode === 'on') {
    cachedLowEnd = false
    return cachedLowEnd
  }

  // 1. 用户明确要求减少动效
  const prefersReducedMotion =
    window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false

  // 2. CPU 逻辑核心数
  const cores = navigator.hardwareConcurrency ?? 8
  const lowCpu = cores < MIN_CPU_CORES

  // 3. 设备内存（仅部分浏览器支持）
  const memory = (navigator as Navigator & { deviceMemory?: number }).deviceMemory
  const lowMemory = typeof memory === 'number' && memory < MIN_DEVICE_MEMORY_GB

  // 4. 软件渲染（无可用 GPU）
  const softwareGpu = isSoftwareWebGL()

  cachedLowEnd =
    prefersReducedMotion || lowCpu || lowMemory || softwareGpu
  return cachedLowEnd
}

/**
 * 自适应性能 Hook。
 *
 * 返回 enableHeavyEffect：是否应启用重度动效。
 * - 初始值来自静态预判。
 * - 若预判通过，会持续采样 FPS；窗口内平均 FPS 过低则永久降级。
 *
 * 用法：
 *   const { enableHeavyEffect } = useAdaptivePerf()
 *   return enableHeavyEffect ? <Prism /> : <StaticBackground />
 */
export function useAdaptivePerf() {
  const [enableHeavyEffect, setEnableHeavyEffect] = useState(
    () => !detectLowEndDevice(),
  )

  const rafRef = useRef<number>(0)
  const frameCountRef = useRef(0)
  const windowStartRef = useRef(0)

  useEffect(() => {
    // 已降级则不必再监控
    if (!enableHeavyEffect) return
    // 部署人员强制开启时，跳过 FPS 兜底降级
    if (getPrismMode() === 'on') return

    let mounted = true

    const tick = (now: number) => {
      if (!mounted) return
      if (windowStartRef.current === 0) windowStartRef.current = now
      frameCountRef.current += 1

      const elapsed = now - windowStartRef.current
      if (elapsed >= FPS_SAMPLE_MS) {
        const fps = (frameCountRef.current * 1000) / elapsed
        if (fps < FPS_DOWNGRADE_THRESHOLD) {
          setEnableHeavyEffect(false)
          cachedLowEnd = true // 记住本次会话的判定
          return
        }
        // 重置采样窗口，持续监控
        frameCountRef.current = 0
        windowStartRef.current = now
      }
      rafRef.current = requestAnimationFrame(tick)
    }

    rafRef.current = requestAnimationFrame(tick)
    return () => {
      mounted = false
      if (rafRef.current) cancelAnimationFrame(rafRef.current)
    }
  }, [enableHeavyEffect])

  return { enableHeavyEffect }
}

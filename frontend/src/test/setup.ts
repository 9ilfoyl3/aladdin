// Vitest 全局 setup：注入 jest-dom 断言与 jsdom 缺失的浏览器 API 垫片。
//
// jsdom 不实现 ResizeObserver / matchMedia / canvas getContext，
// 力导向图组件（GraphCanvas）与部分 UI 依赖它们，这里统一垫片，
// 使组件在测试环境可挂载而不崩溃。

import '@testing-library/jest-dom/vitest'
import { afterEach, vi } from 'vitest'
import { cleanup } from '@testing-library/react'

// 每个用例后清理 DOM，避免相互污染。
afterEach(() => {
  cleanup()
})

// ResizeObserver 垫片：GraphCanvas 用它监听容器尺寸。
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver = ResizeObserverStub as unknown as typeof ResizeObserver

// matchMedia 垫片（部分 UI 组件/库会读取）。
if (!window.matchMedia) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }))
}

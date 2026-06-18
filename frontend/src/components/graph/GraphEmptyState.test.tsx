// GraphEmptyState 冒烟测试（design.md 5.3.4；Requirements 6.6）。
//
// 纯展示组件，验证三种态文案与重试回调，确保空数据/不可用时有明确提示而非崩溃。

import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import GraphEmptyState from './GraphEmptyState'

describe('GraphEmptyState', () => {
  it('unavailable 态显示服务不可用文案', () => {
    render(<GraphEmptyState variant="unavailable" />)
    expect(screen.getByText('知识图谱服务暂不可用')).toBeInTheDocument()
  })

  it('empty 态显示暂无数据；building 时显示构建中', () => {
    const { rerender } = render(<GraphEmptyState variant="empty" />)
    expect(screen.getByText('暂无图谱数据')).toBeInTheDocument()

    rerender(<GraphEmptyState variant="empty" building />)
    expect(screen.getByText('图谱构建中')).toBeInTheDocument()
  })

  it('error 态优先展示传入 message', () => {
    render(<GraphEmptyState variant="error" message="连接超时" />)
    expect(screen.getByText('连接超时')).toBeInTheDocument()
  })

  it('提供 onRetry 时渲染重试按钮并可点击', async () => {
    const onRetry = vi.fn()
    render(<GraphEmptyState variant="error" onRetry={onRetry} />)

    await userEvent.click(screen.getByRole('button', { name: /刷新重试/ }))
    expect(onRetry).toHaveBeenCalledOnce()
  })
})

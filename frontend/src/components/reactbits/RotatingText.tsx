import { useEffect, useState } from 'react'
import './RotatingText.css'

interface RotatingTextProps {
  texts: string[]
  interval?: number
  className?: string
}

// 垂直 fade 轮播文字：当前词淡出上移、下一个词从下方淡入
export default function RotatingText({ texts, interval = 2600, className = '' }: RotatingTextProps) {
  const [index, setIndex] = useState(0)

  useEffect(() => {
    if (texts.length <= 1) return
    const timer = setInterval(() => {
      setIndex((i) => (i + 1) % texts.length)
    }, interval)
    return () => clearInterval(timer)
  }, [texts.length, interval])

  return (
    <span className={`rotating-text ${className}`}>
      {/* 用最长文案占位撑开宽高，避免布局抖动 */}
      <span className="rotating-text-sizer" aria-hidden="true">
        {texts.reduce((a, b) => (b.length > a.length ? b : a), '')}
      </span>
      {texts.map((t, i) => (
        <span key={i} className={`rotating-text-item ${i === index ? 'is-active' : ''}`}>
          {t}
        </span>
      ))}
    </span>
  )
}

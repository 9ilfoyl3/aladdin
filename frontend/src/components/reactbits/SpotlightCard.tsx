import { useRef, type ReactNode, type MouseEvent } from 'react'
import './SpotlightCard.css'

interface SpotlightCardProps {
  children: ReactNode
  className?: string
  spotlightColor?: string
}

// react-bits SpotlightCard：鼠标悬停时跟随光斑高亮的卡片
export default function SpotlightCard({
  children,
  className = '',
  spotlightColor = 'rgba(101, 189, 67, 0.25)',
}: SpotlightCardProps) {
  const divRef = useRef<HTMLDivElement>(null)

  const handleMouseMove = (e: MouseEvent<HTMLDivElement>) => {
    const el = divRef.current
    if (!el) return
    const rect = el.getBoundingClientRect()
    el.style.setProperty('--mouse-x', `${e.clientX - rect.left}px`)
    el.style.setProperty('--mouse-y', `${e.clientY - rect.top}px`)
    el.style.setProperty('--spotlight-color', spotlightColor)
  }

  return (
    <div ref={divRef} onMouseMove={handleMouseMove} className={`card-spotlight ${className}`}>
      {children}
    </div>
  )
}

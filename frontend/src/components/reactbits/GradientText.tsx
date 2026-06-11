import type { ReactNode } from 'react'
import './GradientText.css'

interface GradientTextProps {
  children: ReactNode
  className?: string
  colors?: string[]
  animationSpeed?: number
}

// react-bits GradientText：流动渐变填充的标题文字
export default function GradientText({
  children,
  className = '',
  colors = ['#65bd43', '#3b82f6', '#22c55e', '#65bd43'],
  animationSpeed = 8,
}: GradientTextProps) {
  const gradientStyle = {
    backgroundImage: `linear-gradient(to right, ${colors.join(', ')})`,
    animationDuration: `${animationSpeed}s`,
  }

  return (
    <span className={`gradient-text ${className}`} style={gradientStyle}>
      {children}
    </span>
  )
}

import './ShinyText.css'

interface ShinyTextProps {
  text: string
  disabled?: boolean
  speed?: number
  className?: string
}

// react-bits ShinyText：通过移动的高光渐变让文字呈现扫光效果
export default function ShinyText({ text, disabled = false, speed = 5, className = '' }: ShinyTextProps) {
  const animationDuration = `${speed}s`
  return (
    <span
      className={`shiny-text ${disabled ? 'disabled' : ''} ${className}`}
      style={{ animationDuration }}
    >
      {text}
    </span>
  )
}

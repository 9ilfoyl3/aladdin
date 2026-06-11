import type { ElementType, ComponentPropsWithoutRef } from 'react'
import './StarBorder.css'

type StarBorderProps<T extends ElementType> = {
  as?: T
  className?: string
  color?: string
  speed?: string
  thickness?: number
  children?: React.ReactNode
} & Omit<ComponentPropsWithoutRef<T>, 'as' | 'color'>

// react-bits StarBorder：边框上有两道光点环绕流动的炫酷按钮/容器
export default function StarBorder<T extends ElementType = 'button'>({
  as,
  className = '',
  color = 'white',
  speed = '6s',
  thickness = 1,
  children,
  ...rest
}: StarBorderProps<T>) {
  const Component = (as || 'button') as ElementType

  return (
    <Component
      className={`star-border-container ${className}`}
      style={{
        padding: `${thickness}px 0`,
        ...(rest as { style?: React.CSSProperties }).style,
      }}
      {...rest}
    >
      <div
        className="border-gradient-bottom"
        style={{
          background: `radial-gradient(circle, ${color}, transparent 10%)`,
          animationDuration: speed,
        }}
      />
      <div
        className="border-gradient-top"
        style={{
          background: `radial-gradient(circle, ${color}, transparent 10%)`,
          animationDuration: speed,
        }}
      />
      <div className="inner-content">{children}</div>
    </Component>
  )
}

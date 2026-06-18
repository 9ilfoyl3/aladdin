// 运行时配置（来自 public/config.js，挂载在 window.__APP_CONFIG__ 上）的类型声明

// Prism 动态背景渲染策略
type PrismBackgroundMode = 'auto' | 'on' | 'off'

interface AppConfig {
  /** 登录等页面的动态背景策略：auto 自动降级 / on 强制开 / off 强制关 */
  prismBackground?: PrismBackgroundMode
}

interface Window {
  __APP_CONFIG__?: AppConfig
}

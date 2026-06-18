import Prism from '@/components/Prism'
import { useAdaptivePerf } from '@/lib/perf'

/**
 * 认证类页面（登录/注册/改密/接受邀请）左侧的品牌动态背景。
 *
 * 高端设备渲染 WebGL 的 Prism 动效；低端设备（或运行时 FPS 过低）
 * 自动降级为零开销的 CSS 渐变背景，避免卡顿。
 */
export default function PrismBackground() {
  const { enableHeavyEffect } = useAdaptivePerf()

  if (!enableHeavyEffect) {
    // 低端设备降级：零渲染开销的 CSS 静态背景。
    // 深色基底保证白色品牌文案对比度；多层光晕颜色基于主题色 var(--primary)
    // 经 color-mix 派生，自动跟随主题（绿/橙等）与亮暗模式，与右侧表单协调。
    return (
      <div
        className="h-full w-full"
        style={{
          backgroundColor: '#070708',
          backgroundImage: [
            // 左上主光晕：主题色，较亮
            'radial-gradient(120% 90% at 22% 18%, color-mix(in srgb, var(--primary) 55%, transparent) 0%, transparent 55%)',
            // 右上点缀光晕：主题色，更淡，制造层次
            'radial-gradient(90% 70% at 85% 8%, color-mix(in srgb, var(--primary) 28%, transparent) 0%, transparent 50%)',
            // 整体压暗渐变：左下角最深，确保底部文案清晰
            'linear-gradient(135deg, color-mix(in srgb, var(--primary) 12%, #070708) 0%, #0a0a0c 45%, #050506 100%)',
          ].join(', '),
        }}
      />
    )
  }

  return (
    <Prism
      animationType="rotate"
      timeScale={0.5}
      height={2.5}
      baseWidth={3.5}
      scale={2.6}
      hueShift={0.5}
      colorFrequency={1.5}
      noise={0.5}
      glow={1.5}
    />
  )
}

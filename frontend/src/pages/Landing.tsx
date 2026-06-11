import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  Github,
  Star,
  ArrowRight,
  Workflow,
} from 'lucide-react'
import Lightfall from '@/components/reactbits/Lightfall'
import ShinyText from '@/components/reactbits/ShinyText'
import RotatingText from '@/components/reactbits/RotatingText'
import AgentDemo from '@/components/reactbits/AgentDemo'
import StarBorder from '@/components/reactbits/StarBorder'
import GradientText from '@/components/reactbits/GradientText'
import MagicBento, { type BentoCardData } from '@/components/reactbits/MagicBento'

// GitHub 仓库地址（按需替换为真实仓库）
const GITHUB_URL = 'https://github.com/9ilfoyl3/artoo'

// 能力 Magic Bento 卡片数据（DeerFlow 式错落布局，4列×3行铺满）
const BENTO_CARDS: BentoCardData[] = [
  {
    label: 'ReAct Agent',
    title: '真正的 ReAct Agent',
    description:
      '大模型在 Think → Act → Observe 循环中自主调用工具、分析结果、决定停止时机，而非固定流水线编排。',
    spanClass: 'lg:col-start-1 lg:col-span-2 lg:row-start-1',
  },
  {
    label: 'Retrieval',
    title: '三路混合检索',
    description: 'Dense 语义 + Sparse 稀疏 + BM25 全文并行召回，RRF 融合 + Rerank 精排 + MMR 去冗余 + 父块扩展。',
    spanClass: 'lg:col-start-1 lg:row-start-2',
  },
  {
    label: 'Evidence-First',
    title: 'Evidence-First 纪律',
    description: 'Progressive RAG 提示词强制"先检索、深读 chunk、再作答"，引用溯源、拒绝臆造。',
    spanClass: 'lg:col-start-2 lg:row-start-2',
  },
  {
    label: 'Tools & MCP',
    title: '可扩展工具生态',
    description:
      '内置知识检索、关键词匹配、深度阅读、附件阅读、网页搜索、思考、技能加载等工具，并支持接入远程 MCP Server，按需渐进式加载技能。',
    spanClass: 'lg:col-start-3 lg:col-span-2 lg:row-start-1 lg:row-span-2',
  },
  {
    label: 'Governance',
    title: '多租户与权限治理',
    description: '固定角色 + 归属轴 RBAC，知识库私有 / 组织可见 + 点对点共享，超级管理员、邀请注册与审计日志。',
    spanClass: 'lg:col-start-1 lg:col-span-2 lg:row-start-3',
  },
  {
    label: 'Free & Open Source',
    title: '开源免费 · 私有可控',
    description:
      'MIT 协议开源，自托管、完全自主可控。所有 AI 推理通过 HTTP 调用外部服务，后端轻量，支持内网离线部署。',
    spanClass: 'lg:col-start-3 lg:col-span-2 lg:row-start-3',
  },
]

// Hero 标题轮播文案：让 Artoo ___
const HERO_PHRASES = [
  '自己去检索',
  '自主编排工具',
  '先取证据再作答',
  '读懂你的文档',
  '沉淀团队知识',
]

// 顶部导航
function Nav({ stars }: { stars: number | null }) {
  return (
    <nav className="fixed inset-x-0 top-0 z-50 border-b border-white/10 bg-white/2 backdrop-blur-xs">
      <div className="mx-auto flex h-16 max-w-6xl items-center justify-between px-6">
        <div className="flex items-center gap-2">
          <span className="font-serif text-xl font-semibold tracking-tight text-white">Artoo</span>
        </div>
        <div className="flex items-center gap-3">
          <StarBorder
            as="a"
            href={GITHUB_URL}
            target="_blank"
            rel="noreferrer"
            color="#65bd43"
            speed="5s"
            className="star-border-compact"
          >
            <span className="flex items-center gap-2 text-sm font-medium">
              <Github className="h-4 w-4" />
              Star on GitHub
              {stars !== null && (
                <span className="ml-1 flex items-center gap-1 rounded-full bg-white/10 px-2 py-0.5 text-xs">
                  <Star className="h-3 w-3 fill-current text-yellow-400" />
                  {stars >= 1000 ? `${(stars / 1000).toFixed(1)}k` : stars}
                </span>
              )}
            </span>
          </StarBorder>
        </div>
      </div>
    </nav>
  )
}

export default function Landing() {
  const [stars, setStars] = useState<number | null>(null)

  // 拉取 GitHub Star 数（失败则静默忽略）
  useEffect(() => {
    const repo = GITHUB_URL.replace('https://github.com/', '')
    fetch(`https://api.github.com/repos/${repo}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d && typeof d.stargazers_count === 'number') setStars(d.stargazers_count)
      })
      .catch(() => {})
  }, [])

  return (
    <div className="relative min-h-screen overflow-x-hidden bg-[#070708] text-white">
      <Nav stars={stars} />

      {/* Hero 区 */}
      <section className="relative flex min-h-screen items-center justify-center overflow-hidden px-6">
        {/* Lightfall 动态背景 */}
        <div className="absolute inset-0">
          <Lightfall
            colors={['#27ff84', '#0affe3', '#27ff84']}
            backgroundColor="#0affe3"
            speed={0.6}
            streakCount={3}
            glow={1.1}
            zoom={1.3}
            backgroundGlow={0.1}
            mouseInteraction
            mouseRadius={0.2}
          />
        </div>
        {/* 底部渐隐 */}
        <div className="absolute inset-x-0 bottom-0 h-40 bg-linear-to-t from-[#070708] to-transparent" />

        <div className="relative z-10 mx-auto max-w-4xl text-center">
          <a
            href={GITHUB_URL}
            target="_blank"
            rel="noreferrer"
            className="mb-8 inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/5 px-4 py-1.5 text-sm backdrop-blur-sm transition-colors hover:bg-white/10"
          >
            <span className="flex h-2 w-2 rounded-full bg-[#65bd43]" />
            <ShinyText text="开源 · MIT License · 可私有化部署" speed={4} />
          </a>

          <h1 className="text-5xl font-semibold leading-[1.1] tracking-tight sm:text-6xl md:text-7xl">
            让 <span className="font-serif font-semibold">Artoo</span>
            <br />
            <RotatingText texts={HERO_PHRASES} interval={2600} />
          </h1>

          <p className="mx-auto mt-6 max-w-2xl text-base leading-relaxed text-white/60 sm:text-lg">
            <span className="font-serif font-semibold">Artoo</span> 以 ReAct Agent 为核心，让大模型自主编排关键词检索、语义检索、深度阅读、网页搜索与
            MCP 工具，构建"先检索证据、再作答"的可追溯问答体验。
          </p>

          <div className="mt-10 flex flex-col items-center justify-center gap-4 sm:flex-row">
            <Link
              to="/login"
              className="group flex items-center gap-2 rounded-[20px] bg-white px-7 py-[15px] text-sm font-medium text-black shadow-lg transition-all hover:bg-white/90"
            >
              开始使用
              <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-1" />
            </Link>
          </div>
        </div>
      </section>

      {/* Live Agent 演示 */}
      <AgentDemo />

      {/* 能力 + ReAct 循环（Magic Bento） */}
      <section className="relative mx-auto max-w-6xl px-6 py-24">
        <div className="mb-16 text-center">
          <p className="mb-3 flex items-center justify-center gap-2 text-sm font-medium uppercase tracking-widest text-[#65bd43]">
            <Workflow className="h-4 w-4" />
            Core Capabilities
          </p>
          <h2 className="text-3xl font-semibold tracking-tight sm:text-4xl">
            为可追溯问答而生的能力栈
          </h2>
        </div>

        <MagicBento
          cards={BENTO_CARDS}
          glowColor="39, 255, 132"
          enableTilt
          enableMagnetism
          enableStars
          enableSpotlight
          enableBorderGlow
          clickEffect
        />
      </section>

      {/* CTA：加入社群 */}
      <section className="relative px-6 py-16">
        <div className="mx-auto max-w-3xl text-center">
          <h2 className="text-3xl font-semibold tracking-tight sm:text-5xl">
            <GradientText animationSpeed={7} colors={['#65bd43', '#22c55e', '#3b82f6', '#65bd43']}>
              和我们一起共建
            </GradientText>
          </h2>
          <p className="mx-auto mt-6 max-w-xl text-white/55">
            欢迎提交 Issue 与 Pull Request，分享你的想法，一起打磨更强的 Agentic RAG。每一次贡献，都在让
            <span className="font-serif font-semibold"> Artoo </span>
            变得更好。
          </p>
          <div className="mt-10 flex flex-col items-center justify-center gap-4 sm:flex-row">
            <StarBorder as="a" href={GITHUB_URL} target="_blank" rel="noreferrer" color="#65bd43" speed="5s">
              <span className="flex items-center gap-2 font-medium">
                <Github className="h-5 w-5" />
                Contribute Now
              </span>
            </StarBorder>
          </div>
        </div>
      </section>

      {/* 页脚 */}
      <footer className="border-t border-white/5 px-6 py-10">
        <div className="mx-auto flex max-w-6xl flex-col items-center justify-between gap-4 text-sm text-white/40 sm:flex-row">
          <div className="flex items-center gap-2">
            <span className="font-serif font-semibold text-white/70">Artoo</span>
            <span>— ReAct Agent 驱动的 Agentic RAG 框架</span>
          </div>
          <div className="flex items-center gap-6">
            <a href={GITHUB_URL} target="_blank" rel="noreferrer" className="transition-colors hover:text-white">
              GitHub
            </a>
            <span>MIT License</span>
          </div>
        </div>
      </footer>
    </div>
  )
}

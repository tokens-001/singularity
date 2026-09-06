import { useState } from 'react'
import { Cpu, Bot, Wrench } from 'lucide-react'
import ModelsTab from '../components/ModelsTab'
import AgentsTab from '../components/AgentsTab'
import SkillsTab from '../components/SkillsTab'

const TABS = [
  { key: 'models', icon: Cpu, label: '模型目录' },
  { key: 'agents', icon: Bot, label: '智能体' },
  { key: 'skills', icon: Wrench, label: '技能' },
]

export const ALL_ROLES = ['builder','ai_architect','ai_engineer','qa_engineer','security_auditor','reviewer','generic']
export const ROLE_LABELS: Record<string,string> = { builder:'构建者', ai_architect:'AI架构师', ai_engineer:'AI工程师', qa_engineer:'QA工程师', security_auditor:'安全审计师', reviewer:'审查者', generic:'通用' }
const VENDOR_DISPLAY: Record<string,string> = {
  deepseek: 'DeepSeek', kimi: 'Kimi', claude: 'Claude', moonshot: 'Moonshot',
  glm: 'GLM', gpt: 'GPT', qwen: 'Qwen', openai: 'OpenAI', anthropic: 'Anthropic',
}

// 统一模型名显示：厂商名规范映射 + 连字符转空格 + 每段首字母大写
export const modelDisplay = (id: string) => {
  if (!id) return id
  const parts = id.split('-')
  const vendor = parts[0].toLowerCase()
  const head = VENDOR_DISPLAY[vendor] || (parts[0].charAt(0).toUpperCase() + parts[0].slice(1))
  const rest = parts.slice(1).map(p => p ? p.charAt(0).toUpperCase() + p.slice(1) : p)
  // 连续纯数字段用点号连接（claude-opus-4-8 → 4.8）
  const joined: string[] = []
  for (const p of rest) {
    const prev = joined[joined.length - 1]
    if (prev && /^\d+$/.test(prev) && /^\d+$/.test(p)) joined[joined.length - 1] = prev + '.' + p
    else joined.push(p)
  }
  return [head, ...joined].join(' ')
}
export const mcn = (m:any) => modelDisplay(m.id) || m.display || m.id

export default function Config() {
  const [tab, setTab] = useState('models')
  return (
    <div style={{ maxWidth: 1000, margin: '0 auto' }}>
      <div style={{ display: 'flex', gap: 4, marginBottom: 12 }}>
        {TABS.map(t => (
          <button key={t.key} onClick={() => setTab(t.key)}
            style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '6px 14px', borderRadius: 'var(--radius)',
              border: 'none', cursor: 'pointer', fontSize: 13, fontWeight: tab===t.key?600:400,
              background: tab===t.key?'var(--accent)':'var(--bg-secondary)',
              color: tab===t.key?'#fff':'var(--text-secondary)' }}>
            <t.icon size={14}/> {t.label}
          </button>
        ))}
      </div>
      {tab === 'models' && <ModelsTab />}
      {tab === 'agents' && <AgentsTab />}
      {tab === 'skills' && <SkillsTab />}
    </div>
  )
}

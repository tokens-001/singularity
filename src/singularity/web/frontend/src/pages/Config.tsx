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

export const ALL_ROLES = ['builder','architect','ai_architect','qa_engineer','security_auditor','reviewer','daily']
export const ROLE_LABELS: Record<string,string> = { builder:'构建者', architect:'架构师', ai_architect:'AI架构师', qa_engineer:'QA工程师', security_auditor:'安全审计师', reviewer:'审查者', daily:'日常' }
const MODEL_CN: Record<string,string> = {
  'claude-opus-4-8':'Claude Opus 4.8','deepseek-v4-pro':'DeepSeek V4 Pro','deepseek-chat':'深度求索 V3 Chat',
  'glm-5.2':'智谱 GLM-5.2','glm-5-turbo':'智谱 GLM-5 Turbo',
  'gpt-5.5':'GPT-5.5','gpt-5.5-pro':'GPT-5.5 Pro',
  'kimi-k2.5':'Kimi K2.5','kimi-k2.6':'Kimi K2.6','kimi-k2.7-code':'Kimi K2.7 Code','kimi-k2.7-code-highspeed':'Kimi K2.7 Code 高速',
  'qwen3-coder-plus':'通义千问 Coder Plus','qwen3-coder-next':'通义千问 Coder Next',
  'qwen3.7-max':'通义千问 3.7 Max','qwen3.7-plus':'通义千问 3.7 Plus','qwen3-flash':'通义千问 Flash',
}
export const mcn = (m:any) => MODEL_CN[m.id] || m.display || m.id

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

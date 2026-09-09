import { FolderOpen, ChevronDown } from 'lucide-react'

export type ExecMode = 'auto_edit' | 'confirm_changes'

const chipStyle = { display: 'flex', alignItems: 'center', gap: 6, background: '#fff', border: '1px solid #e5e2d8', borderRadius: 999, padding: '5px 14px', fontSize: 12, color: '#6b6b68', maxWidth: 340, cursor: 'pointer' } as const

/** 工作目录 chip + 执行模式选择。空态和对话态都要用，抽出来消重。 */
export function ChatOptions({ workdir, execMode, onModeChange, onPickRoot }: {
  workdir?: string
  execMode: ExecMode
  onModeChange: (m: ExecMode) => void
  onPickRoot: () => void
}) {
  return (
    <>
      {workdir && (
        <div onClick={onPickRoot} title="项目保存位置（点击更改）" style={chipStyle}>
          <FolderOpen size={13} color="#9a9993" style={{ flexShrink: 0 }}/>
          <span className="truncate">{workdir}</span>
        </div>
      )}
      <div style={{ position: 'relative', display: 'inline-flex', alignItems: 'center' }}>
        <select value={execMode} onChange={e => onModeChange(e.target.value as ExecMode)}
          style={{ appearance: 'none', WebkitAppearance: 'none', background: '#fff',
            border: '1px solid #e5e2d8', borderRadius: 999, padding: '5px 30px 5px 14px',
            fontSize: 12, color: '#6b6b68', cursor: 'pointer', outline: 'none', lineHeight: 1.4 }}>
          <option value="auto_edit">⚡ 自动编辑</option>
          <option value="confirm_changes">🔒 逐步确认</option>
        </select>
        <ChevronDown size={13} color="#9a9993" style={{ position: 'absolute', right: 11, pointerEvents: 'none' }}/>
      </div>
    </>
  )
}

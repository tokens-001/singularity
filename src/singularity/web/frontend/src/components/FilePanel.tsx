import { useState, useEffect } from 'react'
import { useAppStore } from '../stores/app'
import { api } from '../lib/api'
import { useToast } from '../components/Toast'
import { FileText, Folder, FolderOpen, X, PanelRightClose, GitBranch, Download } from 'lucide-react'

interface FileNode {
  name: string
  path: string
  isDir: boolean
  children?: FileNode[]
}

export default function FilePanel({ onClose }: { onClose: () => void }) {
  const [tree, setTree] = useState<FileNode[]>([])
  const [selectedFile, setSelectedFile] = useState<string>('')
  const [fileContent, setFileContent] = useState<string>('')
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [activeTab, setActiveTab] = useState<'files'|'diff'>('files')
  const [diffData, setDiffData] = useState<{stat: string, diff: string}>({stat:'',diff:''})
  const activePid = useAppStore(s => s.activeProjectId)

  useEffect(() => {
    if (activePid === '_default') return
    fetch(`/api/projects/${activePid}/files`).then(r => r.json()).then(d => {
      if (d.files) {
        const nodes = buildTree(d.files)
        setTree(nodes)
        setExpanded(new Set(nodes.map(n => n.path)))
      }
    }).catch(() => { useToast.getState().add('加载文件列表失败', 'error') })
    fetch(`/api/projects/${activePid}/diff`).then(r => r.json()).then(d => {
      setDiffData({ stat: d.stat || '', diff: d.diff || '' })
    }).catch(() => { useToast.getState().add('加载diff失败', 'error') })
  }, [activePid])

  const buildTree = (files: string[]): FileNode[] => {
    const root: FileNode[] = []
    const map: Record<string, FileNode> = {}
    for (const f of files) {
      const parts = f.split('/')
      let current = root
      let currentPath = ''
      for (let i = 0; i < parts.length; i++) {
        const name = parts[i]
        const isLast = i === parts.length - 1
        currentPath = currentPath ? `${currentPath}/${name}` : name
        if (!map[currentPath]) {
          const node: FileNode = { name, path: currentPath, isDir: !isLast }
          map[currentPath] = node
          if (!isLast) node.children = []
          current.push(node)
        }
        if (!isLast) current = map[currentPath].children!
        else current = root
      }
    }
    return root
  }

  const toggleDir = (path: string) => {
    setExpanded(prev => {
      const next = new Set(prev)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      return next
    })
  }

  const downloadFile = () => {
    if (!selectedFile || !fileContent) return
    const blob = new Blob([fileContent], { type: 'text/plain' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url; a.download = selectedFile.split('/').pop() || 'file'
    a.click(); URL.revokeObjectURL(url)
  }

  const openFile = async (path: string) => {
    setSelectedFile(path)
    setFileContent('加载中...')
    try {
      const r = await fetch(`/api/projects/${activePid}/files/${path}`)
      const d = await r.json()
      setFileContent(d.content || '(空文件)')
    } catch {
      setFileContent('加载失败')
    }
  }

  const renderTree = (nodes: FileNode[], depth: number = 0): any => {
    return nodes.map(n => {
      if (n.isDir) {
        const open = expanded.has(n.path)
        return (
          <div key={n.path}>
            <div onClick={() => toggleDir(n.path)}
              style={{ display: 'flex', alignItems: 'center', gap: 4, padding: '2px 4px', paddingLeft: 8 + depth * 14,
                cursor: 'pointer', fontSize: 12, color: 'var(--text-secondary)' }}>
              {open ? <FolderOpen size={12}/> : <Folder size={12}/>}
              <span>{n.name}</span>
            </div>
            {open && n.children && renderTree(n.children, depth + 1)}
          </div>
        )
      }
      return (
        <div key={n.path} onClick={() => openFile(n.path)}
          style={{ display: 'flex', alignItems: 'center', gap: 4, padding: '2px 4px', paddingLeft: 8 + depth * 14,
            cursor: 'pointer', fontSize: 12, color: selectedFile === n.path ? 'var(--accent)' : 'var(--text-secondary)',
            background: selectedFile === n.path ? 'var(--bg-tertiary)' : 'transparent' }}>
          <FileText size={12}/>
          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{n.name}</span>
        </div>
      )
    })
  }

  if (activePid === '_default') {
    return (
      <div style={{ width: 280, background: 'var(--bg-secondary)', borderLeft: '1px solid var(--border)', display: 'flex', flexDirection: 'column', fontSize: 12, color: 'var(--text-muted)', padding: 20, textAlign: 'center' }}>
        选择一个项目查看文件
      </div>
    )
  }

  return (
    <div style={{ width: 320, background: 'var(--bg-secondary)', borderLeft: '1px solid var(--border)', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 2, padding: '6px 8px', borderBottom: '1px solid var(--border)' }}>
        <button onClick={() => setActiveTab('files')} style={{
          padding: '3px 8px', border: 'none', borderRadius: 4, cursor: 'pointer', fontSize: 11,
          background: activeTab === 'files' ? 'var(--bg-tertiary)' : 'transparent',
          color: activeTab === 'files' ? 'var(--text-primary)' : 'var(--text-muted)',
        }}><FileText size={12} style={{display:'inline',marginRight:4}}/>文件</button>
        <button onClick={() => setActiveTab('diff')} style={{
          padding: '3px 8px', border: 'none', borderRadius: 4, cursor: 'pointer', fontSize: 11,
          background: activeTab === 'diff' ? 'var(--bg-tertiary)' : 'transparent',
          color: activeTab === 'diff' ? 'var(--text-primary)' : 'var(--text-muted)',
        }}><GitBranch size={12} style={{display:'inline',marginRight:4}}/>Diff</button>
        <span style={{ flex: 1 }} />
        <button onClick={onClose} style={{ background: 'none', border: 'none', color: 'var(--text-secondary)', cursor: 'pointer', padding: 2 }}>
          <PanelRightClose size={14}/>
        </button>
      </div>

      {activeTab === 'files' && (
        <>
          {/* 文件树 */}
          <div style={{ flex: 1, overflow: 'auto', paddingBottom: 4 }}>
            {tree.length === 0 && <div style={{ padding: 20, textAlign: 'center', color: 'var(--text-muted)', fontSize: 11 }}>暂无文件</div>}
            {renderTree(tree)}
          </div>
          {/* 文件内容预览 */}
          {selectedFile && (
            <div style={{ borderTop: '1px solid var(--border)', maxHeight: '40%', display: 'flex', flexDirection: 'column' }}>
              <div style={{ padding: '4px 10px', fontSize: 10, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'center', gap: 6 }}>
                <span style={{ flex: 1 }}>{selectedFile}</span>
                <button onClick={downloadFile} style={{ background: 'none', border: 'none', color: 'var(--accent)', cursor: 'pointer', padding: 2 }} title="下载">
                  <Download size={12}/>
                </button>
              </div>
              <pre style={{ flex: 1, overflow: 'auto', padding: '8px 10px', margin: 0, fontSize: 11, fontFamily: 'var(--font-mono)',
                color: 'var(--text-primary)', whiteSpace: 'pre-wrap', lineHeight: 1.5 }}>
                {fileContent}
              </pre>
            </div>
          )}
        </>
      )}

      {activeTab === 'diff' && (
        <div style={{ flex: 1, overflow: 'auto', padding: '8px 10px' }}>
          {diffData.stat ? (
            <>
              <pre style={{ fontSize: 10, fontFamily: 'var(--font-mono)', color: 'var(--text-muted)', whiteSpace: 'pre-wrap', marginBottom: 8 }}>
                {diffData.stat}
              </pre>
              <pre style={{ fontSize: 10, fontFamily: 'var(--font-mono)', color: 'var(--text-primary)', whiteSpace: 'pre-wrap', lineHeight: 1.4 }}>
                {diffData.diff || '(无详细差异)'}
              </pre>
            </>
          ) : (
            <div style={{ color: 'var(--text-muted)', fontSize: 11, textAlign: 'center', padding: 20 }}>无变更记录</div>
          )}
        </div>
      )}
    </div>
  )
}

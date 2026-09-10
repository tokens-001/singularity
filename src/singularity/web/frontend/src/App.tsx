import { lazy, Suspense, type ReactNode } from 'react'
import { Routes, Route } from 'react-router-dom'
import { Spin } from 'antd'
import AppLayout from './components/AppLayout'
import { ErrorBoundary } from './components/ErrorBoundary'
import Chat from './pages/Chat'

// Chat 是落地页，保持同步加载；其余三个按需加载（配置页是 antd 控件最重的一页）
const Tasks = lazy(() => import('./pages/Tasks'))
const Projects = lazy(() => import('./pages/Projects'))
const Usage = lazy(() => import('./pages/Usage'))
const Config = lazy(() => import('./pages/Config'))

/** 只把内容区包进 Suspense —— 否则切页时整个侧边栏会被 fallback 顶掉 */
const Page = ({ children }: { children: ReactNode }) => (
  <Suspense fallback={<div style={{ display: 'flex', justifyContent: 'center', padding: 48 }}><Spin /></div>}>
    {children}
  </Suspense>
)

export default function App() {
  return (
    <ErrorBoundary>
      <Routes>
        <Route element={<AppLayout />}>
          <Route path="/" element={<Chat />} />
          <Route path="/tasks" element={<Page><Tasks /></Page>} />
          <Route path="/projects" element={<Page><Projects /></Page>} />
          <Route path="/usage" element={<Page><Usage /></Page>} />
          <Route path="/config" element={<Page><Config /></Page>} />
        </Route>
      </Routes>
    </ErrorBoundary>
  )
}

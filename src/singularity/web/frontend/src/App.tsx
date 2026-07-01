import { Routes, Route } from 'react-router-dom'
import AppLayout from './components/AppLayout'
import { ErrorBoundary } from './components/ErrorBoundary'
import Chat from './pages/Chat'
import Tasks from './pages/Tasks'
import Projects from './pages/Projects'
import Config from './pages/Config'

export default function App() {
  return (
    <ErrorBoundary>
      <Routes>
        <Route element={<AppLayout />}>
          <Route path="/" element={<Chat />} />
          <Route path="/tasks" element={<Tasks />} />
          <Route path="/projects" element={<Projects />} />
          <Route path="/config" element={<Config />} />
        </Route>
      </Routes>
    </ErrorBoundary>
  )
}

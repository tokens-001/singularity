import { Routes, Route, Navigate } from 'react-router-dom'
import AppLayout from './components/AppLayout'
import TaskPanel from './pages/TaskPanel'
import ObserverChat from './pages/ObserverChat'
import AgentDashboard from './pages/AgentDashboard'
import ProjectPipeline from './pages/ProjectPipeline'

export default function App() {
  return (
    <Routes>
      <Route element={<AppLayout />}>
        <Route path="/" element={<Navigate to="/tasks" replace />} />
        <Route path="/tasks" element={<TaskPanel />} />
        <Route path="/observer" element={<ObserverChat />} />
        <Route path="/agents" element={<AgentDashboard />} />
        <Route path="/projects/:id/pipeline" element={<ProjectPipeline />} />
      </Route>
    </Routes>
  )
}

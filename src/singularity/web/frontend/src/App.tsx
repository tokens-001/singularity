import { Routes, Route } from 'react-router-dom'
import AppLayout from './components/AppLayout'
import TaskPanel from './pages/TaskPanel'
import TaskDetail from './pages/TaskDetail'
import ObserverChat from './pages/ObserverChat'
import ProjectList from './pages/ProjectList'
import ProjectPipeline from './pages/ProjectPipeline'
import AgentDashboard from './pages/AgentDashboard'
import ModelManagement from './pages/ModelManagement'
import Settings from './pages/Settings'
import Dashboard from './pages/Dashboard'

export default function App() {
  return (
    <Routes>
      <Route element={<AppLayout />}>
        <Route path="/" element={<Dashboard />} />
        <Route path="/tasks" element={<TaskPanel />} />
        <Route path="/tasks/:id" element={<TaskDetail />} />
        <Route path="/projects" element={<ProjectList />} />
        <Route path="/projects/:id/pipeline" element={<ProjectPipeline />} />
        <Route path="/observer" element={<ObserverChat />} />
        <Route path="/agents" element={<AgentDashboard />} />
        <Route path="/models" element={<ModelManagement />} />
        <Route path="/settings" element={<Settings />} />
      </Route>
    </Routes>
  )
}

import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { App as AntApp, ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import App from './App'
import { antdTheme } from './lib/theme'
import './index.css'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ConfigProvider theme={antdTheme} locale={zhCN}>
      {/* component={false}: 不额外包一层 div，避免打断 .app-shell 的 100vh 布局 */}
      <AntApp component={false}>
        <BrowserRouter><App /></BrowserRouter>
      </AntApp>
    </ConfigProvider>
  </React.StrictMode>
)

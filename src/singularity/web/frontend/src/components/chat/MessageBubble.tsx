import { memo } from 'react'
import { Bubble } from '@ant-design/x'
import type { ChatMsg } from '../../stores/app'

/** 单条消息气泡。memo：SSE 事件会频繁 setState，消息对象身份没变就不该重渲染。 */
export const MessageBubble = memo(function MessageBubble({ m }: { m: ChatMsg }) {
  const isUser = m.role === 'user'
  return (
    <div className="chat-msg-row" style={{ padding: '4px 0' }}>
      <Bubble placement={isUser ? 'end' : 'start'} variant={isUser ? 'filled' : 'borderless'}
        content={m.content}
        styles={isUser
          ? { content: { background: '#2563eb', color: '#fff', borderRadius: 12, fontSize: 13, whiteSpace: 'pre-wrap' } }
          : { content: { padding: 0, fontSize: 13, lineHeight: 1.7, whiteSpace: 'pre-wrap' } }}/>
    </div>
  )
})

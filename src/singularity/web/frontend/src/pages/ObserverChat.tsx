export default function ObserverChat() {
  return (
    <div style={{ display:'flex',flexDirection:'column',height:'calc(100vh - 80px)' }}>
      <div style={{ flex:1,background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:16,overflow:'auto',marginBottom:12 }}>
        <div style={{ marginBottom:12 }}><span style={{color:'var(--accent)',fontWeight:600}}>Observer</span> <span style={{color:'var(--text-muted)',fontSize:11}}>产品经理模式</span></div>
        <div style={{ background:'var(--bg-tertiary)',padding:'8px 12px',borderRadius:'var(--radius)',marginBottom:8,fontSize:12 }}>你想做什么产品？</div>
        <div style={{ background:'var(--accent)',color:'#fff',padding:'8px 12px',borderRadius:'var(--radius)',marginBottom:8,fontSize:12,marginLeft:40 }}>我要给奇点设计一个新前端</div>
        <div style={{ background:'var(--bg-tertiary)',padding:'8px 12px',borderRadius:'var(--radius)',fontSize:12 }}>好的。当前前端是 Flask 模板渲染，你想改造成什么风格？</div>
      </div>
      <div style={{ display:'flex',gap:8 }}>
        <input placeholder="输入消息..." style={{ flex:1,background:'var(--bg-tertiary)',border:'1px solid var(--border)',borderRadius:'var(--radius)',padding:'8px 12px',color:'var(--text-primary)',fontSize:13 }} />
        <button style={{ background:'var(--accent)',color:'#fff',border:'none',borderRadius:'var(--radius)',padding:'8px 16px',cursor:'pointer',fontSize:13 }}>发送</button>
      </div>
    </div>
  )
}

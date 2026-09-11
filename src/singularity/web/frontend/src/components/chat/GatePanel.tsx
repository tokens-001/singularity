import { memo } from 'react'

const GATE_LABELS: Record<string, string> = { '1': '定义完成·请审核PRD', '2': '架构完成·请审核方案', '3': '验收完成·请审核交付物' }

const Details = memo(function Details({ title, color, children }: { title: string; color: string; children: React.ReactNode }) {
  return (
    <details style={{ background: '#ffffff', border: '1px solid #e5e2d8', borderRadius: 10, marginBottom: 10, overflow: 'hidden' }}>
      <summary style={{ cursor: 'pointer', padding: '12px 14px', fontSize: 13, fontWeight: 700, color, listStyle: 'none', display: 'flex', alignItems: 'center', gap: 8, userSelect: 'none' }}>
        <span>{title}</span>
        <span style={{ marginLeft: 'auto', color: '#6b6b68', fontSize: 11, fontWeight: 400 }}>点击展开 ▾</span>
      </summary>
      <div style={{ padding: '0 14px 14px' }}>{children}</div>
    </details>
  )
})

export const ResearchReport = memo(function ResearchReport({ report }: { report: any }) {
  const products = report.competitive_analysis?.products || []
  const pitfalls: string[] = report.pitfalls || []
  return (
    <Details title="📋 调研报告" color="#2563eb">
      {report.recommendation && (
        <div style={{ marginBottom: 10 }}>
          <div style={{ fontSize: 11, color: '#6b6b68', fontWeight: 600, marginBottom: 4 }}>推荐方案</div>
          <div style={{ fontSize: 12, color: '#141413', lineHeight: 1.6 }}>{report.recommendation}</div>
        </div>
      )}
      {products.length > 0 && (
        <div style={{ marginBottom: 10 }}>
          <div style={{ fontSize: 11, color: '#6b6b68', fontWeight: 600, marginBottom: 4 }}>竞品分析</div>
          {products.map((p: any, i: number) => (
            <div key={i} style={{ fontSize: 12, color: '#141413', padding: '6px 8px', background: '#faf9f5', borderRadius: 6, marginBottom: 4, lineHeight: 1.5 }}>
              <b style={{ color: '#141413' }}>{p.name}</b> <span style={{ color: '#6b6b68' }}>· {p.type}</span><br/>
              <span style={{ color: '#16a34a' }}>优：</span>{p.strengths}
            </div>
          ))}
        </div>
      )}
      {pitfalls.length > 0 && (
        <div>
          <div style={{ fontSize: 11, color: '#6b6b68', fontWeight: 600, marginBottom: 4 }}>关键坑</div>
          {pitfalls.map((p, i) => (
            <div key={i} style={{ fontSize: 12, color: '#dc2626', lineHeight: 1.5, marginBottom: 2 }}>⚠ {p}</div>
          ))}
        </div>
      )}
    </Details>
  )
})

export const ArchitectureDetails = memo(function ArchitectureDetails({ arch }: { arch: any }) {
  const modules = arch.modules || []
  const tasks = arch.tasks || []
  const entities = arch.data_model?.entities || []
  const relationships = arch.data_model?.relationships || []
  const contracts = arch.api_contracts || []
  const constraints = arch.constraints || []
  const risks = arch.risks || []
  const sectionTitle = { fontSize: 11, color: '#6b6b68', fontWeight: 600, marginBottom: 4 } as const
  const row = { fontSize: 11, color: '#141413', padding: '3px 0', borderBottom: '1px solid #f3f2ec' } as const
  return (
    <Details title="🏗 架构方案" color="#7c3aed">
      {arch.architecture && (
        <div style={{ fontSize: 12, color: '#141413', lineHeight: 1.6, marginBottom: 10, padding: '8px 10px', background: '#faf9f5', borderRadius: 6 }}>
          {arch.architecture}
        </div>
      )}
      {modules.length > 0 && (
        <div style={{ marginBottom: 10 }}>
          <div style={{ ...sectionTitle, marginBottom: 6 }}>模块（{modules.length}）</div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            {modules.map((m: any, i: number) => (
              <span key={i} style={{ fontSize: 11, color: '#141413', background: '#faf9f5', border: '1px solid #e5e2d8', borderRadius: 6, padding: '3px 9px' }}>{m.name}</span>
            ))}
          </div>
        </div>
      )}
      {tasks.length > 0 && (
        <div>
          <div style={sectionTitle}>任务（{tasks.length}）</div>
          {tasks.map((t: any, i: number) => (
            <div key={i} style={{ fontSize: 11, color: '#141413', padding: '4px 0', borderBottom: '1px solid #f3f2ec' }}>
              <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                <span style={{ color: '#6b6b68', fontFamily: 'monospace' }}>{t.id}</span>
                <span style={{ flex: 1 }}>{t.title}</span>
                <span style={{ fontSize: 10, color: t.complexity === 'high' ? '#dc2626' : t.complexity === 'medium' ? '#b45309' : '#16a34a' }}>{t.complexity}</span>
              </div>
              {(t.depends_on?.length > 0 || t.acceptance) && (
                <div style={{ fontSize: 10, color: '#6b6b68', marginTop: 2 }}>
                  {t.depends_on?.length > 0 && <span>依赖：{t.depends_on.join(', ')}</span>}
                  {t.depends_on?.length > 0 && t.acceptance && <span> · </span>}
                  {t.acceptance && <span>验收：{t.acceptance}</span>}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
      {entities.length > 0 && (
        <div style={{ marginBottom: 10 }}>
          <div style={sectionTitle}>数据模型（{entities.length}）</div>
          {entities.map((e: any, i: number) => (
            <div key={i} style={{ fontSize: 11, color: '#141413', padding: '4px 8px', background: '#faf9f5', borderRadius: 6, marginBottom: 4 }}>
              <b style={{ color: '#141413' }}>{e.name}</b> <span style={{ color: '#6b6b68' }}>（{e.fields?.length || 0} 字段）</span> {(e.fields || []).map((f: any) => f.name).join(', ')}
            </div>
          ))}
          {relationships.length > 0 && (
            <div style={{ fontSize: 11, color: '#6b6b68', lineHeight: 1.6 }}>
              <b>关系：</b>{relationships.map((r: any, i: number) => (
                <span key={i}>{r.from}→{r.to}({r.type}) </span>
              ))}
            </div>
          )}
        </div>
      )}
      {contracts.length > 0 && (
        <div>
          <div style={sectionTitle}>API 契约（{contracts.length}）</div>
          {contracts.map((a: any, i: number) => (
            <div key={i} style={row}>
              <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                <span style={{ color: '#16a34a', fontFamily: 'monospace', fontWeight: 600, minWidth: 36 }}>{a.method}</span>
                <span style={{ color: '#2563eb', fontFamily: 'monospace' }}>{a.path}</span>
                <span style={{ color: '#6b6b68', flex: 1 }}>{a.description}</span>
              </div>
              {(a.input || a.output) && (
                <div style={{ fontSize: 10, color: '#6b6b68', paddingLeft: 44, marginTop: 2 }}>
                  {a.input && <div>入参：{JSON.stringify(a.input)}</div>}
                  {a.output && <div>返回：{JSON.stringify(a.output)}</div>}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
      {arch.tech_stack && (
        <div style={{ marginBottom: 10 }}>
          <div style={sectionTitle}>技术栈</div>
          {Object.entries(arch.tech_stack).map(([k, v]: [string, any], i: number) => (
            <div key={i} style={{ fontSize: 11, color: '#141413', padding: '3px 0' }}>
              <b style={{ color: '#6b6b68' }}>{k}：</b>{v}
            </div>
          ))}
        </div>
      )}
      {constraints.length > 0 && (
        <div style={{ marginBottom: 10 }}>
          <div style={sectionTitle}>约束（{constraints.length}）</div>
          {constraints.map((c: any, i: number) => (
            <div key={i} style={row}>
              <span style={{ color: '#b45309', fontWeight: 600 }}>[{c.type}]</span> {c.rule}
              {c.check && <span style={{ color: '#6b6b68' }}> → 验证：{c.check}</span>}
            </div>
          ))}
        </div>
      )}
      {risks.length > 0 && (
        <div>
          <div style={sectionTitle}>风险（{risks.length}）</div>
          {risks.map((r: any, i: number) => (
            <div key={i} style={row}>
              <span style={{ color: r.impact === 'high' ? '#dc2626' : r.impact === 'medium' ? '#b45309' : '#16a34a', fontWeight: 600 }}>[{r.impact}]</span> {r.risk}
              {r.mitigation && <span style={{ color: '#6b6b68' }}> → {r.mitigation}</span>}
            </div>
          ))}
        </div>
      )}
    </Details>
  )
})

const secTitle = { fontSize: 11, color: '#6b6b68', fontWeight: 600, marginBottom: 4 } as const

/** GATE3 验收摘要。以前这道门只显示几周前的调研/架构，QA 报告生成完没人看得见。
 *  摘要行刻意放在折叠框外：不展开也能判断该不该过。 */
export const AcceptancePanel = memo(function AcceptancePanel({ acceptance, projectIssues }: { acceptance: any; projectIssues?: any[] }) {
  const qa = acceptance?.qa_report
  const conf = acceptance?.conformance
  const sum = qa?.summary || {}
  const issues: any[] = qa?.issues || []
  const failed = sum.failed ?? issues.length
  const qaOk = !!qa && failed === 0 && sum.verdict !== 'no_go'
  // 无参调用时后端如实返回"无法核验——这不是通过"，不能当绿灯显示
  const unverifiable = conf?.evidence?.unverifiable === true
  const confColor = !conf ? '#6b6b68' : unverifiable ? '#b45309' : conf.passed ? '#16a34a' : '#dc2626'
  const confLabel = !conf ? '—' : unverifiable ? '无法核验' : conf.passed ? '通过' : '不通过'

  return (
    <div style={{ marginBottom: 10 }}>
      <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap', alignItems: 'center', background: '#ffffff', border: '1px solid #e5e2d8', borderRadius: 10, padding: '10px 14px', fontSize: 12 }}>
        <span style={{ fontWeight: 700, color: qa ? (qaOk ? '#16a34a' : '#dc2626') : '#6b6b68' }}>
          QA：{!qa ? '无报告' : qaOk ? '通过' : `${failed} 个问题`}
        </span>
        <span style={{ fontWeight: 700, color: confColor }}>需求符合性：{confLabel}</span>
        {!qa && <span style={{ color: '#6b6b68', fontSize: 11 }}>（本次没生成 QA 报告，只按下面的明细判）</span>}
      </div>
      {/* 项目 issues 放在折叠框外：验收被跳过这类事必须一眼看见，不能再是静默的 */}
      {(projectIssues || []).length > 0 && (
        <div style={{ marginTop: 8, background: '#fffdf5', border: '1px solid #e8dcc0', borderRadius: 10, padding: '10px 14px' }}>
          {(projectIssues as any[]).map((it: any, i: number) => (
            <div key={i} style={{ fontSize: 11, color: '#b45309', lineHeight: 1.6 }}>
              ⚠ {it.detail || it.reason || it.message || JSON.stringify(it)}
            </div>
          ))}
        </div>
      )}
      {(issues.length > 0 || conf?.reason) && (
        <details style={{ background: '#ffffff', border: '1px solid #e5e2d8', borderRadius: 10, marginTop: 8, overflow: 'hidden' }}>
          <summary style={{ cursor: 'pointer', padding: '12px 14px', fontSize: 13, fontWeight: 700, color: '#0f766e', listStyle: 'none', display: 'flex', alignItems: 'center', gap: 8, userSelect: 'none' }}>
            <span>🔍 验收明细</span>
            <span style={{ marginLeft: 'auto', color: '#6b6b68', fontSize: 11, fontWeight: 400 }}>点击展开 ▾</span>
          </summary>
          <div style={{ padding: '0 14px 14px' }}>
            {issues.length > 0 && (
              <div style={{ marginBottom: 10 }}>
                <div style={secTitle}>问题（{issues.length}）</div>
                {issues.map((it: any, i: number) => (
                  <div key={i} style={{ fontSize: 11, color: '#141413', padding: '4px 0', borderBottom: '1px solid #f3f2ec' }}>
                    <span style={{ fontWeight: 600, color: it.severity === 'critical' ? '#dc2626' : it.severity === 'warning' ? '#b45309' : '#6b6b68' }}>[{it.severity || 'info'}]</span>
                    {it.fix_route && <span style={{ color: '#6b6b68', marginLeft: 6 }}>→ 回{it.fix_route === 'design' ? '架构' : it.fix_route === 'impl' ? '实现' : it.fix_route}</span>}
                    <div style={{ lineHeight: 1.5, marginTop: 2 }}>{it.description}</div>
                    {it.file && <div style={{ color: '#6b6b68', fontFamily: 'monospace' }}>{it.file}</div>}
                  </div>
                ))}
              </div>
            )}
            {conf?.reason && (
              <div>
                <div style={secTitle}>需求符合性</div>
                <div style={{ fontSize: 12, color: confColor, lineHeight: 1.6 }}>{conf.reason}</div>
              </div>
            )}
          </div>
        </details>
      )}
    </div>
  )
})

interface Props {
  info: any
  gateNum: string
  gatePhase: string
  acceptance?: any
  onGate: (decision: 'approved' | 'rejected') => void
}

/** GATE 审核面板（含调研报告 / 架构方案）。memo：任务日志高频更新时不该重渲染这棵大树。 */
export const GatePanel = memo(function GatePanel({ info, gateNum, gatePhase, acceptance, onGate }: Props) {
  const isGate3 = gateNum === '3'
  return (
    <div style={{ padding: '8px 0', textAlign: 'center' }}>
      <div style={{ display: 'inline-flex', alignItems: 'center', gap: 8, background: '#eaf6ec', border: '1px solid #16a34a', borderRadius: 8, padding: '8px 16px' }}>
        <span style={{ fontSize: 13, fontWeight: 600, color: '#16a34a' }}>🛑 GATE{gateNum}</span>
        <span style={{ fontSize: 12, color: '#6b6b68' }}>{GATE_LABELS[gateNum] || '等待审核'}</span>
        <button onClick={() => onGate('approved')}
          style={{ background: '#16a34a', color: '#141413', border: 'none', borderRadius: 4, padding: '3px 10px', fontSize: 11, fontWeight: 600, cursor: 'pointer' }}>✅ 通过</button>
        <button onClick={() => onGate('rejected')}
          style={{ background: '#b5b2a8', color: '#dc2626', border: 'none', borderRadius: 4, padding: '3px 10px', fontSize: 11, cursor: 'pointer' }}>↩ 打回</button>
      </div>
      <div style={{ maxWidth: 760, margin: '12px auto 0', textAlign: 'left' }}>
        {/* GATE3 要审的是交付物，先给验收结论；下面那两坨是项目历史，排后面 */}
        {isGate3 && <AcceptancePanel acceptance={acceptance} projectIssues={info.issues} />}
        {info.research_report && <ResearchReport report={info.research_report} />}
        {info.architecture && <ArchitectureDetails arch={info.architecture} />}
      </div>
    </div>
  )
})

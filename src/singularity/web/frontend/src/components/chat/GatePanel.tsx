import { memo, useState } from 'react'

const GATE_LABELS: Record<string, string> = { '1': '定义完成·请审核PRD', '2': '架构完成·请审核方案', '3': '验收完成·请审核交付物' }

/** GATE2 **兼任两个完全不同的角色**，而长文案以前按门号写死 ⇒ 两种处境长得一模一样。
 *
 *  · **初次审架构** —— 架构刚出来，人看一眼再放行；
 *  · **出事后的兜底点** —— `integrating` 里「审查自动修已达上限(2轮), 升 GATE2 人工兜底」，
 *    升上来请人拍板。真机 2026-09-15 用户当场问：**「为什么任务都执行失败了，还有架构审核」**。
 *
 * 理由**一直躺在盘上**（`lineage` 里最后一次进 gate2 的那条 `reason`），**界面不读它**
 * ⇒ 人不可能知道为什么又被问了一次。这里读出来。
 *
 * ⚠️ 判据用 **`owner_confirm.gate2 === 'approved'`（本门批过 ⇒ 这次是二次进来）**，
 * 而不是去匹配 `reason` 的措辞 —— 文案会变，状态不会。
 * ⚠️ 短标签（`Projects.tsx` 的 `gate2:'G2确认'`）写死不碍事，**长文案写死才是误导**。
 */
export function gateCopy(gateNum: string, info: any): { label: string; reason?: string } {
  if (gateNum !== '2') return { label: GATE_LABELS[gateNum] || '等待审核', reason: '' }
  if (info?.owner_confirm?.gate2 !== 'approved') return { label: GATE_LABELS['2'], reason: '' }
  const entries = Array.isArray(info?.lineage) ? info.lineage : []
  const back = [...entries].reverse().find((e: any) => e?.to === 'gate2')
  return {
    label: '⚠️ 出事后升上来的人工兜底 —— 这次不是初次审架构',
    reason: (back && back.reason) || '',
  }
}

const Details = memo(function Details({ title, color, children, open = false }:
  { title: string; color: string; children: React.ReactNode; open?: boolean }) {
  return (
    <details open={open} style={{ background: '#ffffff', border: '1px solid #e5e2d8', borderRadius: 10, marginBottom: 10, overflow: 'hidden' }}>
      <summary style={{ cursor: 'pointer', padding: '12px 14px', fontSize: 13, fontWeight: 700, color, listStyle: 'none', display: 'flex', alignItems: 'center', gap: 8, userSelect: 'none' }}>
        <span>{title}</span>
        <span style={{ marginLeft: 'auto', color: '#6b6b68', fontSize: 11, fontWeight: 400 }}>点击展开 ▾</span>
      </summary>
      <div style={{ padding: '0 14px 14px' }}>{children}</div>
    </details>
  )
})

/** 调研报告顶层键 → 中文名。
 *
 * ⚠️ **查不到就原样显示键名**（下面用的是 `RESEARCH_LABELS[k] ?? k`）——
 * 模型以后多吐一段必须**自动出现**。这份报告以前只露 3/8 段，根因就是
 * "没写进代码的段永远看不见"，别再犯一次。
 */
const RESEARCH_LABELS: Record<string, string> = {
  recommendation: '推荐方案',
  scope_clarification: '范围澄清',
  competitive_analysis: '竞品分析',
  frontier_theory: '前沿理论',
  user_research: '用户调研',
  technical_poc: '技术验证',
  constraints: '约束',
  pitfalls: '关键坑',
}

/** 只决定**排序**，不决定**出现与否**。不在表里的键排到最后（靠索引 99）。 */
const RESEARCH_ORDER = ['recommendation', 'scope_clarification', 'competitive_analysis',
  'frontier_theory', 'user_research', 'technical_poc', 'constraints', 'pitfalls']

const MUTED = { fontSize: 11, color: '#6b6b68' } as const
const SECTION_TITLE = { fontSize: 11, color: '#6b6b68', fontWeight: 600, marginBottom: 4 } as const
const PRE = {
  fontSize: 11, color: '#141413', background: '#faf9f5', borderRadius: 6, padding: 8,
  maxHeight: 320, overflow: 'auto', whiteSpace: 'pre-wrap', wordBreak: 'break-word', margin: 0,
} as const

/** 一句摘要 —— 按**值的形状**分支，**不按键名写死**（换个键名一样有摘要）。 */
function summarize(v: any): string {
  if (v == null || v === '') return '（空）'
  if (typeof v === 'string') {
    const s = v.replace(/\s+/g, ' ').trim()
    return s.length > 80 ? `${s.slice(0, 80)}…` : s
  }
  if (Array.isArray(v)) return `${v.length} 条`
  if (typeof v === 'object') {
    const names = Object.keys(v)
    return names.length ? `${names.length} 项：${names.slice(0, 4).join(' / ')}` : '（空对象）'
  }
  return String(v)
}

/** 通用正文渲染。**兜底永远是 JSON，绝不能是空** ——
 *  少了那句，`competitive_analysis: {}`（没有 products）点开就是一个空框，
 *  跟"这段本来就没内容"长得一模一样（防御模式 #77.10 的形状：报"没有"而实际是"没渲染"）。 */
function renderValue(value: any, depth = 0): React.ReactNode {
  if (value == null || value === '') return <div style={MUTED}>（空）</div>
  if (typeof value === 'string') {
    return <div style={{ fontSize: 12, lineHeight: 1.6 }}>{value}</div>
  }
  if (typeof value !== 'object') return <div style={{ fontSize: 12 }}>{String(value)}</div>
  if (Array.isArray(value)) {
    if (!value.length) return <div style={MUTED}>（空）</div>
    const allStr = value.every((x: any) => typeof x === 'string')
    if (allStr) {
      return <div>{value.map((s: string, i: number) =>
        <div key={i} style={{ fontSize: 12, lineHeight: 1.6, marginBottom: 2 }}>• {s}</div>)}</div>
    }
    return <pre style={PRE}>{JSON.stringify(value, null, 2)}</pre>
  }
  const entries = Object.entries(value)
  if (!entries.length) return <div style={MUTED}>（空对象）</div>
  return (
    <div>
      {entries.map(([k, v]) => (
        <div key={k} style={{ marginBottom: depth ? 6 : 10 }}>
          <div style={SECTION_TITLE}>{k}</div>
          {renderValue(v, depth + 1)}
        </div>
      ))}
    </div>
  )
}

/** 一段的正文。保留两处**更好读**的渲染（竞品产品卡 / 关键坑红字），其余走通用渲染。 */
function SectionBody({ name, value }: { name: string; value: any }) {
  if (name === 'competitive_analysis' && Array.isArray(value?.products) && value.products.length) {
    const { products, ...rest } = value
    return (<>
      <div style={{ marginBottom: 8 }}>
        {products.map((p: any, i: number) => (
          <div key={i} style={{ fontSize: 12, color: '#141413', padding: '6px 8px', background: '#faf9f5', borderRadius: 6, marginBottom: 4, lineHeight: 1.5 }}>
            <b style={{ color: '#141413' }}>{p.name}</b> <span style={{ color: '#6b6b68' }}>· {p.type}</span><br/>
            <span style={{ color: '#16a34a' }}>优：</span>{p.strengths}
          </div>
        ))}
      </div>
      {/* ⚠️ 剩下的子块（`comparison` / `differentiation`）**不能被吞掉** ——
          它们在这一段里，以前从来没露过面。产品卡只是"更好读"，不是"这一段的全部"。 */}
      {Object.keys(rest).length > 0 && renderValue(rest)}
    </>)
  }
  if (name === 'pitfalls' && Array.isArray(value) && value.length) {
    return (
      <div>{value.map((p: string, i: number) => (
        <div key={i} style={{ fontSize: 12, color: '#dc2626', lineHeight: 1.5, marginBottom: 2 }}>⚠ {p}</div>
      ))}</div>
    )
  }
  return <>{renderValue(value)}</>
}

export const ResearchReport = memo(function ResearchReport(
  { report, projectId }: { report: any; projectId?: string }) {
  // 🔴 **解析失败必须说出来**（2026-09-17 真机，用户原话「我怎么不能看报告」）。
  //
  // 模型吐的 JSON 坏了（字符串里带裸换行）⇒ 后端 `try_parse_json` 走兜底
  // `{raw_output: 前 5000 字, parse_error: true}` ⇒ 底下那三个分支**一个都不进**
  // ⇒ **渲染成一个空框**（标题还在、点开是空的，跟"还没有数据"长得一模一样）。
  // ⚠️ 而这里原来**压根不认识 `parse_error`** ⇒ **一个字都不提示**。
  if (report?.parse_error) {
    const raw: string = report.raw_output || ''
    return (
      // `open`：解析失败**一进来就要看见**。默认收起的话，用户在第一道门上
      // 看到的只是一个"📋 调研报告 ⚠"的折条 —— 跟"有报告、还没点开"没区别，
      // 而事实是"这份报告根本解不开"。要说的那句必须自己走出来。
      <Details title="📋 调研报告 ⚠" color="#dc2626" open>
        <div style={{ fontSize: 12, color: '#dc2626', lineHeight: 1.6, marginBottom: 8 }}>
          ⚠ 报告解析失败 —— 模型输出的不是合法 JSON。下面这段是原文开头。
        </div>
        {report.raw_truncated && (
          <div style={{ fontSize: 11, color: '#6b6b68', marginBottom: 8 }}>
            这里只有前 {raw.length} 字，原文共 {report.raw_chars} 字。
          </div>
        )}
        {projectId && (
          <div style={{ marginBottom: 8 }}>
            <a href={`/api/projects/${projectId}/research-raw`} target="_blank" rel="noreferrer"
               style={{ fontSize: 12, color: '#2563eb' }}>📄 打开完整原文</a>
          </div>
        )}
        <pre style={{
          fontSize: 11, color: '#141413', background: '#faf9f5', borderRadius: 6, padding: 8,
          maxHeight: 320, overflow: 'auto', whiteSpace: 'pre-wrap', wordBreak: 'break-word',
        }}>{raw || '（原文也是空的）'}</pre>
      </Details>
    )
  }

  // ── 正常报告：**每段一行**，默认全部收起，点哪段展开哪段 ──
  //
  // 2026-09-17 用户提的：「一次显示所有方案太多，改为每个方案的名称摘要，
  // 我自己选择要不要点开看」。原来是**三块固定内容写死**（推荐方案/竞品/关键坑），
  // 调研报告实际有 8 段 —— 另外 5 段（前沿理论/用户调研/范围澄清/技术验证/约束）
  // **从来没露过面**，用户根本不知道它们存在。
  //
  // ⚠️ 外层**不套折叠框**：套了的话，看任何一段都要点两下（先展开外层、再展开那段）。
  const obj = (report && typeof report === 'object' && !Array.isArray(report)) ? report : null
  if (!obj) return <div style={MUTED}>📋 调研报告：（格式不认识）</div>
  const rank = (k: string) => { const i = RESEARCH_ORDER.indexOf(k); return i < 0 ? 99 : i }
  const keys = Object.keys(obj).sort((a, b) => rank(a) - rank(b))
  return (
    <div style={{ marginBottom: 10 }}>
      <div style={{ ...SECTION_TITLE, fontSize: 12, marginBottom: 6 }}>📋 调研报告（{keys.length} 段）</div>
      {keys.map(k => (
        <details key={k} style={{ border: '1px solid #f3f2ec', borderRadius: 6, marginBottom: 4 }}>
          <summary style={{ cursor: 'pointer', padding: '8px 10px', fontSize: 12, listStyle: 'none',
                            display: 'flex', gap: 8, alignItems: 'center', userSelect: 'none' }}>
            <b style={{ color: '#141413', whiteSpace: 'nowrap' }}>{RESEARCH_LABELS[k] ?? k}</b>
            <span style={{ color: '#6b6b68', flex: 1, overflow: 'hidden',
                           textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{summarize(obj[k])}</span>
            <span style={{ color: '#6b6b68', fontSize: 11, whiteSpace: 'nowrap' }}>展开 ▾</span>
          </summary>
          <div style={{ padding: '2px 10px 10px' }}>
            <SectionBody name={k} value={obj[k]} />
          </div>
        </details>
      ))}
    </div>
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
                <span style={{ color: '#6b6b68', fontFamily: 'var(--font-mono)'}}>{t.id}</span>
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
                <span style={{ color: '#16a34a', fontFamily: 'var(--font-mono)', fontWeight: 600, minWidth: 36 }}>{a.method}</span>
                <span style={{ color: '#2563eb', fontFamily: 'var(--font-mono)'}}>{a.path}</span>
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
              {/* check 有两种：{argv, expect_exit}（机器能跑）或一段散文（验不了）。
                  直接渲染对象会让 React 抛错，所以这里必须分开处理。
                  机器能跑的标出来 —— 它是"信任上限"那个数的分子。 */}
              {typeof c.check === 'object' && c.check?.argv ? (
                <span style={{ color: '#166534' }}>
                  {' '}→ <code>{c.check.argv.join(' ')}</code>
                  <span style={{ color: '#6b6b68' }}>（期望退出码 {c.check.expect_exit ?? 0}）</span>
                  {/* 这句必须写出来：批准架构 = 批准这些命令被实际执行。
                      不写的话，"人批准过"就是句空话。 */}
                  <span style={{ color: '#166534', fontWeight: 600 }}> ✓可机器验（批准后验收时会实际执行这条命令）</span>
                </span>
              ) : c.check ? (
                <span style={{ color: '#6b6b68' }}> → 验证（人工）：{String(c.check)}</span>
              ) : null}
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
  // ⚠️ **"读不出来"不是"通过"**（2026-09-14 外派反审抓到的真洞 —— `a33bf79` 引入的）。
  // 后端读不出 `qa_report.json` 时给的是占位符 `{"error": …}`：**它是真值**，
  // 于是 `!!qa` 成立、`sum` 落成 `{}`、`failed` 算成 0 ⇒ 渲染**绿色「通过」**。
  // **改之前（`None`）反而是灰色「无报告」** —— 那次"修"把它从灰爬成了绿，而它就坐在人审门上。
  // 判据：**真报告一定带 `summary`**（见 `validator.build_qa_report` 的返回字面量），
  // 缺了就是"核不了"。与隔壁 `conformance` 的 `unverifiable → 琥珀色` 同口径 ——
  // 同一个"我核不了"，不该一边给琥珀、一边给绿灯。
  const qaUnverifiable = !!qa && !qa.summary
  const qaOk = !!qa && !qaUnverifiable && failed === 0 && sum.verdict !== 'no_go'
  // 无参调用时后端如实返回"无法核验——这不是通过"，不能当绿灯显示
  const unverifiable = conf?.evidence?.unverifiable === true
  const confColor = !conf ? '#6b6b68' : unverifiable ? '#b45309' : conf.passed ? '#16a34a' : '#dc2626'
  const confLabel = !conf ? '—' : unverifiable ? '无法核验' : conf.passed ? '通过' : '不通过'

  return (
    <div style={{ marginBottom: 10 }}>
      <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap', alignItems: 'center', background: '#ffffff', border: '1px solid #e5e2d8', borderRadius: 10, padding: '10px 14px', fontSize: 12 }}>
        <span style={{ fontWeight: 700, color: qa ? (qaUnverifiable ? '#b45309' : qaOk ? '#16a34a' : '#dc2626') : '#6b6b68' }}>
          QA：{!qa ? '无报告' : qaUnverifiable ? '核不了' : qaOk ? '通过' : `${failed} 个问题`}
        </span>
        <span style={{ fontWeight: 700, color: confColor }}>需求符合性：{confLabel}</span>
        {!qa && <span style={{ color: '#6b6b68', fontSize: 11 }}>（本次没生成 QA 报告，只按下面的明细判）</span>}
        {qaUnverifiable && (
          <span style={{ color: '#b45309', fontSize: 11 }}>
            {qa.error || '（报告里没有 summary，内容可能是空的）'}
          </span>
        )}
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
                    {it.file && <div style={{ color: '#6b6b68', fontFamily: 'var(--font-mono)'}}>{it.file}</div>}
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
  /** `feedback` = 打回理由（选填）。会一路带到重跑的调研/架构提示词里。 */
  onGate: (decision: 'approved' | 'rejected', feedback?: string) => void
}

const Bar = ({ items, tone = 'normal' }: { items: (string | null | undefined)[]; tone?: 'normal' | 'warn' }) => {
  const shown = items.filter(Boolean) as string[]
  if (!shown.length) return null
  return (
    <div style={{
      display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center',
      background: tone === 'warn' ? '#fffdf5' : '#ffffff',
      border: `1px solid ${tone === 'warn' ? '#e8dcc0' : '#e5e2d8'}`,
      borderRadius: 10, padding: '9px 14px', marginBottom: 8, fontSize: 12,
      color: tone === 'warn' ? '#b45309' : '#141413', lineHeight: 1.5,
    }}>
      {shown.map((s, i) => <span key={i}>{s}</span>)}
    </div>
  )
}

/** 每道门的摘要 —— **放在折叠框外面**：不点开就能判断该不该过。
 *  以前三道门长一个样（两个折叠框 + 两个按钮），要判断只能全部展开读一遍。 */
const GateSummary = memo(function GateSummary({ gateNum, research, arch, projectIssues }: any) {
  if (gateNum === '1' && research) {
    const prods = (research.competitive_analysis?.products || []).length
    const pits: string[] = research.pitfalls || []
    return <Bar items={[
      // 🔴 **解析失败不能说成"调研没给方案"**（2026-09-17 真机）。
      // 兜底对象里**没有** `recommendation`，所以原来那句判据恒假 ⇒ 界面写着
      // 「⚠ 调研没给推荐方案」——**把锅甩给了调研**，而事实是报告解不开、内容一个字没丢。
      // 用户原话：「页面显示调研没给推荐方案，所以我看不到」。
      // 判据要落在"报告坏没坏"上，不是"有没有那个字段"（防御模式 #77）。
      research.parse_error
        ? '⚠ 报告解析失败（不是"调研没给方案"）—— 点开看原文'
        : research.recommendation ? `推荐：${research.recommendation}` : '⚠ 调研没给推荐方案',
      prods ? `${prods} 个竞品` : null,
      pits.length ? `${pits.length} 个坑` : null,
    ]} />
  }
  if (gateNum === '2' && arch) {
    const high = (arch.risks || []).filter((r: any) => r.impact === 'high').length
    const nTasks = (arch.tasks || []).length
    return <Bar tone={nTasks ? 'normal' : 'warn'} items={[
      nTasks ? `${nTasks} 个任务` : '⚠ 架构没拆出任务（下一步会卡住）',
      `${(arch.modules || []).length} 个模块`,
      `${(arch.constraints || []).length} 条约束`,
      high ? `⚠ ${high} 条高风险` : null,
    ]} />
  }
  return null
})

/**
 * 项目档案：架构 / 调研的**随时可看**入口。
 *
 * ⚠️ **它跟 `GatePanel` 是两件事，别合并**：`GatePanel` 本质是**审批条**
 * （`🛑 GATE{n}` + ✅通过 / ↩打回，**按钮是无条件渲染的**）—— 不在闸门时挂它，
 * 会常驻一个**假的 GATE 横幅**和两个**按不动也按不得的按钮**。
 * 材料本来就拆成了独立组件（`ArchitectureDetails` / `ResearchReport`，项目页直接复用），
 * 这里只是把同一份材料在对话页也挂一份。
 *
 * 折叠是白送的：两者内部都是 `<details>`，**默认就是收起的**。
 *
 * 2026-09-15 用户提的：「对话页为什么不能一直显示架构，还得去项目页看」——
 * 原来架构只在 GATE2 / GATE3 那两个窗口露脸，**批完就再也看不见了**。
 */
export const ProjectArchive = memo(function ProjectArchive({ info }: { info: any }) {
  if (!info?.architecture && !info?.research_report) return null
  return (
    <div style={{ maxWidth: 760, margin: '12px auto 0', textAlign: 'left' }}>
      {info.architecture && <ArchitectureDetails arch={info.architecture} />}
      {info.research_report && <ResearchReport report={info.research_report} projectId={info.id} />}
    </div>
  )
})

/** 门禁**那根条**：状态 + 通过/打回（+ 打回理由输入框）。
 *
 * 2026-09-17 从 `GatePanel` 里抽出来 —— 它要挪进**常驻的顶部状态条**。
 * 判据：**门禁本质是「当前状态 + 一个动作」，属于状态条，不属于正文**。
 * 留在正文里的话，用户往下滚一屏就看不见"该我审批了"—— 而这个仓为"该看见的没看见"
 * 栽过太多次（#28 那一族）。
 */
export const GateBar = memo(function GateBar({ info, gateNum, onGate }: Props) {
  const copy = gateCopy(gateNum, info)
  // 打回理由。**点"打回"先展开一个输入框**，而不是弹系统 prompt：
  // 系统 prompt 样式不可控、非浏览器宿主没有，而且 jsdom 里压根不实现（测不了）。
  // 选填 —— 不填就点"确认打回"照样能退回，别让"写理由"变成打回的门槛。
  const [rejecting, setRejecting] = useState(false)
  const [reason, setReason] = useState('')
  const doReject = () => { onGate('rejected', reason); setRejecting(false); setReason('') }
  return (
    <div style={{ textAlign: 'center' }}>
      <div style={{ display: 'inline-flex', alignItems: 'center', gap: 8, background: '#eaf6ec', border: '1px solid #16a34a', borderRadius: 8, padding: '8px 16px' }}>
        <span style={{ fontSize: 13, fontWeight: 600, color: '#16a34a' }}>🛑 GATE{gateNum}</span>
        <span style={{ fontSize: 12, color: copy.reason ? '#b45309' : '#6b6b68' }}>{copy.label}</span>
        <button onClick={() => onGate('approved')}
          style={{ background: '#16a34a', color: '#141413', border: 'none', borderRadius: 4, padding: '3px 10px', fontSize: 11, fontWeight: 600, cursor: 'pointer' }}>✅ 通过</button>
        <button onClick={() => setRejecting(true)}
          style={{ background: '#b5b2a8', color: '#dc2626', border: 'none', borderRadius: 4, padding: '3px 10px', fontSize: 11, cursor: 'pointer' }}>↩ 打回</button>
      </div>
      {rejecting && (
        <div style={{ maxWidth: 760, margin: '8px auto 0', textAlign: 'left',
                      background: '#fffdf5', border: '1px solid #e8dcc0', borderRadius: 8, padding: '10px 12px' }}>
          <div style={{ fontSize: 11, color: '#b45309', marginBottom: 6 }}>
            哪里不满意？（选填）—— 这句会**带给重新跑的那一份**，比"打回重来"有用得多。
          </div>
          <textarea value={reason} onChange={e => setReason(e.target.value)} rows={3}
            placeholder="例：竞品只有 3 家，补到 5 家并给出对比表"
            style={{ width: '100%', boxSizing: 'border-box', fontSize: 12, padding: 6,
                     border: '1px solid #e5e2d8', borderRadius: 6, resize: 'vertical' }} />
          <div style={{ marginTop: 6, display: 'flex', gap: 8 }}>
            <button onClick={doReject}
              style={{ background: '#dc2626', color: '#ffffff', border: 'none', borderRadius: 4, padding: '3px 10px', fontSize: 11, fontWeight: 600, cursor: 'pointer' }}>确认打回</button>
            <button onClick={() => { setRejecting(false); setReason('') }}
              style={{ background: '#e5e2d8', color: '#141413', border: 'none', borderRadius: 4, padding: '3px 10px', fontSize: 11, cursor: 'pointer' }}>取消</button>
          </div>
        </div>
      )}
    </div>
  )
})

/** 门禁要看的**材料**：摘要 / 验收明细 / 调研报告 / 架构。放进「材料」侧滑面板。
 *  ⚠️ 它和 `GateBar` 分开是有意的：条要**钉在视野里**，材料可以滚、可以收起。 */
export const GateBody = memo(function GateBody({ info, gateNum, acceptance }: Props) {
  const isGate3 = gateNum === '3'
  const copy = gateCopy(gateNum, info)
  return (
    <div style={{ textAlign: 'left' }}>
      <div style={{ maxWidth: 760, margin: '0 auto', textAlign: 'left' }}>
        {/* 摘要和"该看的东西"都按门分 —— 三道门以前长一个样，且显示的内容不对：
            GATE1 那时还没有架构却显示"架构方案"；GATE3 要审交付物，却把几周前的
            调研/架构摆在最前面。 */}
        <GateSummary gateNum={gateNum} research={info.research_report}
                     arch={info.architecture} projectIssues={info.issues} />
        {isGate3 && <AcceptancePanel acceptance={acceptance} projectIssues={info.issues} />}
        {/* GATE3 的正文是交付物（上面那块），旧文档降级为"项目背景"排在后面 */}
        {/* 🔴 **GATE1 必须渲染调研报告**（2026-09-17 真机）。
            原来这里是 `gateNum !== '1' && ...` —— 而 GATE1 正是**审调研**的那道门：
            加上 `Chat.tsx` 的 `!isGate` 把 `ProjectArchive` 也藏了 ⇒ **两道门一起把报告挡死**，
            用户在第一道门上**一处都看不到报告**，只剩上面那条摘要（还写着"调研没给方案"）。
            昨夜 `6de62c4` 修的红字解析失败提示，就修在这个**当时不渲染**的组件里。 */}
        {info.research_report && <ResearchReport report={info.research_report} projectId={info.id} />}
        {gateNum === '2' && info.architecture && <ArchitectureDetails arch={info.architecture} />}
        {isGate3 && info.architecture && <ArchitectureDetails arch={info.architecture} />}
        {isGate3 && (
          <div style={{ fontSize: 11, color: '#6b6b68', marginTop: 4 }}>
            下面两份是项目早期的调研与架构，仅供追溯，不是本次要审的交付物。
          </div>
        )}
        {/* 兜底升上来的 GATE2：**把来路摆出来** —— 理由一直在 `lineage` 里，界面以前不读它 */}
        {copy.reason && (
          <div style={{ fontSize: 12, color: '#b45309', background: '#fdf6ec',
                        border: '1px solid #e8c99b', borderRadius: 8,
                        padding: '8px 12px', marginTop: 8, lineHeight: 1.6 }}>
            <b>为什么又问你一次：</b>{copy.reason}
          </div>
        )}
      </div>
    </div>
  )
})

/** 条 + 材料拼起来 —— 老的调用方（和既有测试）不用改。
 *  ⚠️ 顺序不能反：**先看见"该审批了"，再看见"审什么"**。 */
export const GatePanel = memo(function GatePanel(props: Props) {
  return <><GateBar {...props} /><GateBody {...props} /></>
})

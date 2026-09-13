# __rewrite_sample__ —— McpTab 样板重写（外派 ⑥ 的交付物）

**这是什么**：按「统一渲染判据」重写的一个最小 Tab（`McpTab`，原件 65 行，是五个配置 Tab 里最小的），
加两个从它身上长出来、可供其余页面共用的原语（`Async.tsx`）。**不是发布代码** —— 没有任何
现有文件 import 它（`Config.tsx` 用的还是原件），所以它对线上行为零影响；这也是根
`tsconfig.json` / 根 `vite.config.ts` 把本目录 exclude 掉的理由，成立。

**为什么样板选 McpTab 而不是 Alerts**：① 65 行，一次能写对；② 它**没有**测试锁着的既有约定
（Alerts 的「`alert_summary` 缺失按空处理」是 `Alerts.test.tsx` 钉住的兼容约定，重写它会跟
自己的测试打架）；③ 它把要治的病**集齐了**：加载失败被渲染成「没有 MCP 服务器」、
`connected` 缺失被渲染成「未连接」、`tool_count` 缺失会拼出「undefined 工具」、
后端给了 `enabled` 字段界面根本没显示。

---

## 一、四态判据（`Async.tsx` 钉死在类型里）

| 态 | 何时 | 渲染什么 | 反例（旧代码的行为） |
|---|---|---|---|
| loading | 还没拿到答案 | 文字「正在加载…」（`role="status"`）；**头部不显示计数** | 头部显示 `(0)` —— "还没数"被说成"数过是零" |
| error | 拿到了"失败"这个答案 | 红字「加载失败：**后端原因**」+ 重试按钮（`role="alert"`）；**绝不落进空态文案** | 只 toast 一声，原地渲染「没有 MCP 服务器」 |
| ready + 空 | 真拿到数据且 `length === 0` | 灰字「没有…」+ 空态提示（怎么做能不空）；此时 `(0)` **要**显示 | 同左（旧代码空态文案是对的，只是失败时也会走到这） |
| ready | 有数据 | 正文；若再取失败 → 旧数据**保留** + 琥珀条「刷新失败：… 下面还是最近一次成功加载的数据」 | 失败把旧列表吹掉，或失败只有 toast 没有痕迹 |

空态判据是 **`length === 0`，不是真值与否** —— 空数组是真值，`!!data` 会把"没有"渲染成"有"。

资源是复合对象时（本页 = `{servers, tools}` 两个列表），四态跟**主列表**走：把 `Loadable`
切到主列表视角再交给边界（见 `McpTab.tsx` 里的 `listState`），相（loading/error/staleError）原样保留。

### 并发的语义（`useResource` 里的请求序号）

这个前端"挂载 / SSE 再取 / 轮询 / 手动刷新"三路并发是**常态**（dev 下 `<StrictMode>`
每次挂载就真有两路）。所以 `load()` 内部记了 `seqRef`，**过期请求的结果一律作废**：

- 慢的**成功**后到 → 不许盖掉新数据；
- 慢的**失败**后到 → 不许往新数据上贴「刷新失败」——
  ⚠️ 这条尤其要紧：那条横幅写着"下面还是最近一次成功加载的数据"，
  而**旧失败后到时屏幕上恰恰就是最新数据** ⇒ 不作废的话，横幅本身在撒谎。

同仓的标准答案就是 `Chat.fetchSeq` / `Projects.detailSeq`；这个原语第一版漏了它
（2026-09-14 外派⑧反审抓到、我补的，`Async.test.tsx` 三条并发用例钉着）。

## 二、McpTab 字段判据表（缺什么显示什么，逐字段）

后端契约出处：`scheduler/_api_admin.py:384` `mcp_server_list`（8 键恒给，但值可能是空串 ——
来自手编 `mcp_servers.toml`，`mcp.py:522` 对缺键取 `""`；**toml 读不出来时后端静默回默认配置**，
前端看不见这件事，见「已知边界」第 2 条）。

| 字段 | 取值/缺失 | 显示 | 为什么 |
|---|---|---|---|
| `name` | 正常 | 原样 | — |
| | `""`（toml 条目漏 name，真实可能） | **（未命名）** | 渲染成空串 = 一条看不见的行；且空串做 React key 会撞 key |
| `transport` | `"stdio"` / `"http"` / 任何手填词 | **原样回显** | 后端不校验这个自由词 —— 不认识的枚举值 ≠ 空，生词也照念 |
| `command`/`url` | 恰有一个非空 | 原样（mono + truncate，title 留全文） | — |
| | 两个都 `""` | **（未配置启动方式 —— stdio 要 command，http 要 url）** | 渲染成空 = 看不见；这句还告诉你该补哪个字段 |
| `enabled` | `true`/缺失 | 不打扰 | 好端端的不用说话（同用量页对 active 供应商的口径） |
| | `false` | **已停用**（灰点+文字） | 旧版没显示这个字段 —— 主动停用和连不上长得一样，责任说不清 |
| `enabled=false` **且** `connected=true` | 数据自相矛盾 | **配置矛盾**（琥珀） | 矛盾的数据不许静默选一边信 |
| `connected` | `true` | 绿点 +「N 工具」 | — |
| | `false` | 灰点 +「未连接」 | 正常态，不是错 |
| | **缺失**（形状漂移） | **状态未知**（琥珀点+文字） | "不知道连没连"不许说成"没连上" |
| `tool_count` | 数字且已连接 | 「N 工具」 | **0 只在真数过时显示**（未连接时后端本来就给 0，不渲染它） |
| | 已连接但缺失 | **已连接 · 工具数未知** | 旧版会拼出「undefined 工具」；拿 0 充 = 编数字（同 money.ts 的纪律） |
| 头部计数 | ready | `（N）` + 「M 个工具」 | — |
| | loading / error | **不显示计数** | 这时显示 `(0)` 是编的 |
| | ready 且有服务器但 0 工具 | 琥珀「0 个工具 —— 这些服务器没有一个连接成功…」 | 0 要解释它意味着什么 |

配色沿用全站口径（`GatePanel`/`TaskCard`/`money.ts` 同源）：**灰=没数据/正常 · 琥珀=核不了/需注意 · 绿=通过 · 红=失败**。
每一态都有**文字**，圆点只是辅助（`aria-hidden`）—— 不许只靠颜色区分。

## 三、已知边界（样板没治的，别误以为治了）

1. **`api.ts` 的 `||[]` 兜底会吞形状漂移**：`api.mcpServers()` 内部是 `(d?.servers||d||[])` ——
   后端哪天返回 `{}` 或缺键，信息在 api 层就被碾成 `[]`，样板只能拿到一个空数组，
   "接口没给"和"真没有"在组件层已经**无法区分**。样板没改 `api.ts`（本轮不许改现有文件）。
   **铺开方案里单列一步**：给主力 list 函数加显式判据（缺键 → throw 带端点名的形状错误）。
2. **toml 读不出来 ≠ 前端可见**：后端 `load_mcp_configs` 读坏 toml 时打日志后**静默回默认配置**，
   接口照常 200。前端只能如实转述列表。"读坏配置要出声"得在后端修（那侧已有 `load_toml`
   的 S1 纪律，不归本样板）。
3. `POST /api/mcp/refresh` 返回 `{ok, servers, tools}`，旧版把响应整个丢掉。样板把计数
   放进了成功 toast（用后端给的数，不自己数）。`ok:false` 理论上不该出现（后端恒 ok:true），
   出现时按 info toast，不谎报成功。
4. **忘接线 = 永远转圈**：state 初值是 `{ phase: 'loading' }`，调用方若忘了写
   `useEffect(() => { void load(true) }, [load])`，`AsyncBoundary` 就渲染一个**无超时**的
   「正在加载…」—— 正是 #16（Alerts 永远转圈）那个形状被原语原样复活。
   风险低（测试能抓），但铺开时**每接一个页面都得先确认这一句在**。
5. **`load(true)` 与 `load(false)` 是"意图"不是"约束"**：落点由 `prev.phase` 决定，
   交叉情形（如 prev=ready 时误用 `load(true)`）会把已渲染的数据吹回 loading。
   样板自己用 `load(false)` 做刷新避开了，但这是**约定**。
6. **没有 AbortController**：过期请求的结果会被丢弃（见上面的序号），但 **fetch 本身照跑到底**。
   正确性上没问题（React 18+ 卸载后 setState 是 no-op），只是不省流量。

## 四、不新增依赖

用到的东西全部已在 `package.json`：React 19 内置 hooks、`lucide-react`（一个图标）、
现有 `index.css` 的类（`status-dot`/`flex-center`/`fs-*`/`btn-sm`）、现有 `lib/toast.ts` 的
`errText`/`useToast`。测试用 vitest + jsdom 的裸 `createRoot + act` —— 和现有 12 份测试同一 harness，
连 `@testing-library` 都不需要（项目本来就没装）。

**要不要加别的？不要。** 考虑过又放弃的：`@testing-library/react`（查询API 好写，但会分裂
两种 harness 风格，且现有测试全靠 `textContent` 断言——这个"把整页当字符串读"的土办法
恰好是对"每态有文字"最直接的验收）；`swr`/`react-query`（useResource 42 行就够了，
引一个库来管缓存对这个无浏览器、SSE 驱动的界面是负资产）。

## 五、文件清单与提升路径

```
__rewrite_sample__/
  Async.tsx                 原语：Loadable 类型 + useResource + AsyncBoundary（四态边界）
  Async.test.tsx            原语测试 ×10（含 3 条并发）
  McpTab.tsx                样板本体（默认导出，签名与原件一致）
  McpTab.test.tsx           样板测试 ×14（四态 + 字段判据逐条）
  tsconfig.sample.json      样板局部类型检查（根 tsconfig exclude 了本目录，见下）
  vitest.sample.config.ts   样板局部测试收集（根 vite.config exclude 了本目录，见下）
  README.md                 本文件
```

评审通过后**三步提升**（一步一个提交）：

1. `Async.tsx` → `src/components/Async.tsx`；唯一改动：`import { errText } from '../../lib/toast'`
   的相对路径减一级（`../lib/toast`）。带上 `Async.test.tsx`（同样只改路径）。
2. `McpTab.tsx` 的内容覆盖 `src/components/McpTab.tsx`；`../../lib/…` 减一级，
   `./Async` 不变（此时它已在 components/ 里）。**`Config.tsx` 一个字不用动** ——
   默认导出名、props（无 props）都没变。删除 `components/McpTab.test.tsx` 若无同名，
   直接把样板的测试放 `components/McpTab.test.tsx`。
3. 删除整个 `__rewrite_sample__/`，并把根 `tsconfig.json` / `vite.config.ts` 里那两行
   exclude 摘掉（它们就是为这个目录临时加的）。从此样板的测试**进默认收集**，
   成为"前端全绿"的一部分。

## 六、没浏览器怎么验收（本轮实测过的命令）

在 `src/singularity/web/frontend/` 下：

```bash
npx tsc --noEmit                                              # 仓库本体（不含样板）→ 退出码 0
npx tsc --noEmit -p src/pages/__rewrite_sample__/tsconfig.sample.json   # 样板本体 → 退出码 0
npx vitest run                                                # 仓库默认收集（不含样板）→ 73 绿（12 文件）
npx vitest run --config src/pages/__rewrite_sample__/vitest.sample.config.ts __rewrite_sample__
                                                              # 样板 → 24 绿（2 文件）
```

⚠️ **末条那个 `__rewrite_sample__` 过滤参数不能省**（2026-09-14 复核时实测更正）：
`vitest.sample.config.ts` 里的 `root` **不是**配置文件所在目录、仍是 `process.cwd()`（= `frontend/`），
所以**不带过滤参数跑的是全量**：14 文件 / 97 用例（= 默认 73 + 样板 24），不是 24/2。
（配置文件的注释原先写反了，已一并更正。）

⚠️ **为什么样板要单独跑**：根配置把本目录 exclude 了（"还在迭代的东西挂进默认收集，
会让『前端全绿』变成假的"——那是对的），但排除 ≠ 不用查。实测教训：排除生效后
一次真实类型错误（数组约束 vs 复合对象）从 `tsc` 眼皮底下漏了过去，最后在 vitest
运行时以更难看的方式爆出来。**所以两份 tsc + 两份 vitest，四条命令缺一不可。**

**变异自检**（判断据是不是真被锁住了 —— 每一刀都必须让指名的测试变红）：

| 变异（改这一刀） | 必须红的测试 |
|---|---|
| catch 里把失败 setState 成空数据（吞错） | McpTab「🔴 加载失败绝不渲染成『没有 MCP 服务器』」 |
| 删掉 staleError 分支（旧数据丢弃/只 toast） | 两份「有数据后再取失败」 |
| `state.data.length === 0` 改成 `!!state.data` | Async「空数组渲染空态文字」 |
| 删 `connected === undefined` 分支（落灰"未连接"） | McpTab「connected 缺失 → 状态未知」 |
| `tool_count` 判断改成 `s.tool_count \|\| 0` | McpTab「工具数缺 → 工具数未知，不许落 0」 |
| 删 `enabled === false` 分支 | McpTab「enabled=false → 已停用」 |
| 删「配置矛盾」分支 | McpTab「enabled=false 却 connected」 |
| 把 Status 里的文字删掉只留圆点 | 上述所有含文字断言的用例 |

**真机 3 分钟手验清单**（提升到 components/ 后，给上真机的人）：
① 正常打开配置页 → MCP Tab：能列出服务器，`mcp.toml` 里 `enabled=false` 的那条显示「已停用」；
② `kill` 后端 → 切到 MCP Tab：出现红色「加载 MCP 服务器失败：…」+ 重试按钮，**不是**「没有 MCP 服务器」；
③ 点「重新加载配置」：toast 报后端给的 N/M 计数；
④ 把 `mcp.toml` 改坏（删掉一条的 name）→ 重载 → 出现「（未命名）」的行，页面不崩。

## 七、实测记录（2026-09-14 05:2x，HEAD `814e1be` 附近，仓库正被并发提交）

- 样板：`tsc -p tsconfig.sample.json` 退出码 0；vitest **24/24 绿**（2 文件）。
- 仓库默认收集：**73/73 绿**（12 文件），`tsc --noEmit` 退出码 0 —— 样板对现有信号零影响。
- 开发中曾被根配置的 exclude 藏过一条真类型错误（见第六节），已修并写进教训。

# qidian-knowledge —— I 层（校验层）知识检索引擎 · 优化方案

> 参考架构：`.claude/skills/ui-ux-pro-max/`（已逐文件拆解）
> 数据来源：`knowledge/`（23 篇案卷）+ `research/`（insights/decisions/questions/experiments/references，47 篇）
> 服务对象：多 Agent 平台的 **I 层**——D（决策）→ **I（校验）** → E（执行）中间那层硬校验（见 `research/insights/I009`）
> 写于 2026-06-17

---

## 0. 先说清楚：这个引擎是干嘛的

ui-ux-pro-max 检索是为了**生成**（给设计建议）。
这个引擎检索是为了**校验**（判断一个方案/结论是否和已验证的原则、已做的决策、已有的案卷一致）。

方向反了，输出形状就不一样。I 层不要"最匹配的几行"，I 层要的是：

- 这个候选方案**必须满足哪些原则**（P 文件）
- 这件事**以前是不是已经决策过**（D 文件）——方向是否一致（词法级提示）
- 有没有**对应的案卷/前例**（knowledge/）
- 撞上了哪些**已知反例 / 反模式**（P 文件的「反例」段）
- 牵动哪些**未决问题**（Q 文件）

按 I009 的定义，I 层是「规则+schema 的硬校验」，不是 insight。所以这个引擎必须 **确定性、可解释、便宜**——这三条决定了下面所有取舍。

**一条硬约束先立在这：不用向量/embedding。** 70 篇文档上 BM25 + 图谱足够；embedding 会引入 API 成本、延迟、和**非确定性**——一个校验层每次跑出不同结果，等于没校验。这点和 ui-ux-pro-max 一致（它也是纯 BM25），不是偷懒，是对的。

---

## 1. 保留 ui-ux-pro-max 的什么

逐项过了一遍源码，这些骨架是对的，直接留：

| 保留项 | 出处 | 为什么留 |
|--------|------|---------|
| **零依赖 BM25 核** | `core.py` BM25 类 | 确定性、无 API、git 可 diff、便宜。完全契合 I 层「中频、成本可控」（I009 预算结构） |
| **域路由（domain routing）** | `core.py` CSV_CONFIG | 把语料切成有类型的子集，每类有自己的检索列/输出列。我这里改成按文档类型分域 |
| **查询自动判域** | `core.py` detect_domain | 关键词命中数判 domain。逻辑直接复用，词表换成 D/I/P/E/Q 意图 |
| **token 优化输出** | `search.py` format_output | 超长截断 300 字、结构化 markdown 给 LLM 读。I 层吐给上游 Agent 时同样需要 |
| **聚合/推理层模式** | `design_system.py` | 多域检索 → 套规则 → 合成一份报告。我把它从「设计系统生成器」改成「校验报告生成器」 |
| **Master + Overrides 分层持久化** | `design_system.py` persist | 全局规则 + 局部覆盖、局部优先。改用途为「项目级校验档案 + 缓存检索结果」 |
| **SKILL.md 清单模式** | `SKILL.md` | 优先级表 + when-to-apply + CLI 用法。I 层作为一个 skill 挂上去就靠这个 |
| **core / search / 聚合器 三文件分离** | 整个 scripts/ | 关注点分离干净，照搬结构 |

一句话：**搜索引擎的引擎部分（BM25 排序、域路由、输出格式、聚合器骨架）几乎全留。换的是它两头——进料口（分词+摄取）和出料口（校验语义）。**

---

## 2. 改什么以适配我的场景

### 2.1 【最关键】分词器：空格切词 → 中文 ngram 混合

`core.py:109-112` 现在是：
```python
text = re.sub(r'[^\w\s]', ' ', str(text).lower())
return [w for w in text.split() if len(w) > 2]
```
中文没空格，「验证层优先于分析维度」会变成**一个 token**，BM25 直接废。

改成混合分词（思路取自 I011 天搜的「中文 2-4 字 ngram」）：
- 中文连续段：切 2/3/4 字 ngram（「验证层」→ 验证、证层、验证层…）
- ASCII 段：保留原 split（处理 `BM25`、`app.py`、`DeepSeek`、`snake_case`/`CamelCase` 拆分）
- 版本号/编号识别：`P001`、`007`、`v2.0` 当整 token
- 同义词扩展：「验证层 / 校验层 / Validator」归一（天搜的「工程同义词扩展」）

这是整个方案的**第一性改动**，必须先做、先验证，后面全建在它上面。

### 2.2 存储：CSV → Markdown 语料 + 构建索引（JSON 缓存）

ui-ux-pro-max 的知识是手写 CSV 行（一行一条规则）。我的知识是**长文 Markdown**——200 行的案卷塞不进 CSV 单元格，也不该塞。

改法：
- 语料原地不动（`knowledge/`、`research/` 是唯一真相源，手编 markdown）
- 加一个 **摄取层** `ingest.py`：解析 frontmatter（`tags`）、in-body 元数据（`编号/归档日期/定位`）、章节切块、`[[wikilink]]`、状态标记（`自测通过`/`已验证`/`🔄`/`反例`）
- 构建出 **JSON 索引**落到 `data/index/`，BM25 在索引上跑，不每次重扫 markdown
- 真正表格化的参考数据（如有）仍可走 CSV——保留 ui-ux-pro-max 的 CSV 通道作为补充

### 2.3 域定义：style/color/... → 按文档类型分

ui-ux-pro-max 的域是 style/color/typography/...。我的域按**类型+目录前缀**定：

| 域 | 来源 | 在校验中的角色 |
|----|------|---------------|
| `principle` | research/insights/ 的 **P** 文件 | 候选必须满足的原则；带「反例」「适用范围」「状态」 |
| `decision` | research/decisions/ 的 **D** 文件 | 已做的决策；查矛盾的主战场 |
| `insight` | research/insights/ 的 **I** 文件 | 方法论/复盘/审计，软参考 |
| `case` | knowledge/ 23 篇案卷 | 前例、概念定义（委身论、二级维持、完美防御…） |
| `question` | research/questions/ 的 **Q** 文件 | 未决问题，校验时提示「这条还没验证」 |
| `experiment` | research/experiments/ 的 **E** 文件 | 实验状态 |

注意 I009 的坑：这里的 I 域是 **insight（research 里的方法论）**，和 Agent 架构的 **I 层（校验）** 同字不同义。引擎名里用「校验层 / validation」，域名里 insight 归 insight，别混。

### 2.4 出料口：设计建议 → 校验证据包

`design_system.py` 的 `generate()` 现在合成「pattern/style/colors/typography」。改成 `validate.py` 的 `validate(candidate)`，合成一份**校验证据包**：
- 相关原则（要满足的）
- 相关历史决策（查方向一致性——词法级，非语义）
- 相关案卷/前例
- 命中的反例/反模式
- 牵动的未决问题
- **分数 + 明细**（P003「可解释输出」：分数 + 明细，不是黑箱）

`design_system.py` 里那张 `ui-reasoning.csv` 推理规则表 → 换成 `validation_rules.csv` 校验规则表。

---

## 3. 加什么它没有但我需要的

ui-ux-pro-max 完全没有、但 I 层必须有的：

### 3.1 交叉引用图谱 + 邻居扩散
knowledge/ 是真有链接图谱的 DAG，007 是 hub（被 003/004/005/010/012/016/020 七处引用）。research/ 是 Q→E→I→D→P→knowledge 的演化链。
加 `graph.py`：建 `[[wikilink]]` 图，检索命中一个节点时**带出它的邻居**。校验「委身论」时，自动把引用它的案卷一起捞出来。ui-ux-pro-max 的 CSV 行之间没有任何关系，这是从零加。

### 3.2 方向一致性提示（词法级，不冒充矛盾检测）
I 层的核心价值之一，对应 CLAUDE.md 的职责：「当前决策和已归档结论矛盾，且未主动说明原因 → 切进去」。
但诚实地说：不用 embedding 的情况下，能做的是**词法信号**——候选和某历史决策共享主题词但方向词不同（如一方含否定词一方不含），提示人工比对。这不是语义矛盾检测，是字符串级的方向不一致提示。
真正的语义矛盾检测需要 embedding 或 LLM，和本引擎「确定性、零 API」的硬约束冲突。取舍：保留词法提示，标注为「建议人工复核」，不冒充自动矛盾检测。

### 3.3 成熟度 / 状态感知
P 文件几乎全是「自测通过（0真实用户）」，还带「反例」。
I 层**不能拿一条 0 真实用户的原则当硬法律去 block**——否则就是 I010 批的「伪校验」。
加：索引时抽 `状态` 字段，校验输出区分**硬约束（已验证）** vs **软提示（自测通过/待验证）**。撞上未验证原则 → 提示，不阻断。

### 3.4 可解释字段
每条命中返回：匹配到的词、分数拆解、链接路径、命中的规则 id。P003 的「可解释输出」落到字段级。

### 3.5 Schema 校验钩子
I009 说 I 层 = **规则 + schema**。检索只是 I 层的一半，另一半是结构校验（候选是不是合法 JSON schema）。加一个 `schema_gate`，和检索并列——检索喂规则判断，schema 判结构。

### 3.6 评测集（golden set）
取自天搜的 golden set + SkillOpt 的 gate 思路。
20-30 条「查询 → 期望命中文件」的评测集，**先建评测、再调引擎**。没有评测就调分词，等于 I010 的伪校验自己也犯。这一步也为以后 SkillOpt 式自动调优铺路。

### 3.7 增量构建 + 新鲜度检查
语料是手编 markdown，会改。`ingest.py` 按 mtime 增量重建索引，避免每次全扫。

---

## 4. 文件结构

```
python/qidian-knowledge/
├── PLAN.md                     # 本文件
├── SKILL.md                    # 清单：when-to-apply、域、CLI 用法、优先级表
├── config.py                   # 语料根路径、域→目录映射、BM25/图谱权重
├── scripts/
│   ├── tokenizer.py            # ★新 中文 ngram + ASCII + 编号/版本号 + 同义词扩展
│   ├── ingest.py               # ★新 MD→规范化文档（frontmatter/定位/章节/[[link]]/状态）
│   ├── index.py                # ★新 构建/加载 JSON 索引，增量，mtime 新鲜度
│   ├── core.py                 # 改 BM25（换 tokenizer）+ 域路由 + 索引检索
│   ├── search.py               # 改 CLI：query→排序证据，token 优化输出
│   ├── graph.py                # ★新 wikilink 图谱 + 邻居扩散
│   ├── validate.py             # 改自 design_system.py：候选→证据包+词法提示（不评分，三档判定）
│   └── eval.py                 # ★新 golden set 评测（recall@k）
├── data/
│   ├── index/                  # 构建出的 JSON 索引缓存（git 忽略或提交，二选一）
│   ├── synonyms.csv            # ★新 工程/认知同义词扩展表
│   ├── validation_rules.csv    # ★新 校验规则表（对标 ui-reasoning.csv）
│   └── golden_set.jsonl        # ★新 评测查询 + 期望命中
```

语料（`knowledge/`、`research/`）**不动**，作为只读源；引擎读它、把索引建到 `data/index/`。
★新 = ui-ux-pro-max 没有的；改 = 在它源码基础上改。

---

## 5. 实施步骤

按「先验证再扩展」推进（D004 停止开发进入验证 / P001 验证层优先）。每个阶段以**可验证的检查点**收口，过不了不进下一阶段。

**Phase 0 · 摄取骨架**
`ingest.py` 把两套语料解析成规范化文档列表（id/type/tags/定位/sections/links/status）。
✅ 检查点：跑一次输出 `23 + 47 = 70` 篇，frontmatter/前缀/链接抽取正确，随机抽 3 篇人工核对。

**Phase 1 · 中文分词 + BM25（最关键的正确性闸）**
先写 `golden_set.jsonl`（20-30 条），再写 `tokenizer.py` + 接进 `core.py`，用 `eval.py` 量 recall@k。
✅ 检查点：golden set recall@3 过阈值（先定 0.8）。**过不了就别往下建——整个引擎都压在这一层上。** 这一步本身就是对「伪校验」的免疫。

**Phase 2 · 域路由 + 搜索 CLI**
判域词表换成 D/I/P/E/Q 意图，`search.py` 出 token 优化结果。
✅ 检查点：`python scripts/search.py "委身论" --type case` 和 `"验证层" --type principle` 都命中正确文件。

**Phase 3 · 图谱 + 邻居扩散**
`graph.py` 建链接图。
✅ 检查点：查 007 能带出 7 个入链邻居；查 P001 能带出引用它的 D001。

**Phase 4 · 校验模式（I 层本体）**
`validate.py`：词法方向提示 + 状态感知 + 三档判定（注意/人工复核/信息不足），不做伪精确评分。
✅ 检查点：喂一个和某条 D 文件结论相反的候选，引擎标出冲突的 D 文件；喂一个撞 0 真实用户原则的候选，引擎给**软提示**不是硬阻断。

**Phase 5 · 接入 I 层 + schema 闸**
封成 skill，D→I→E 管线调用它，并联 `schema_gate`。
✅ 检查点：一条假的 D→I→E 调用链能跑通，I 层既做检索校验也做结构校验。

**Phase 6 · 评测驱动调优**
- **已接 SkillOpt Gate 模式**（`eval.py --gate`）：改引擎核心文件后自动对比基线，Recall@3 退化则拒绝。基线锁定于 88.24%（30条）。
- **自动 Reflect + Edit 推迟**：SkillOpt 的 Rollout→Reflect→Edit→Gate 全自动循环需要足够真实使用数据。现在引擎刚接上 cognitive-quick-ref 第一个调用方，等真实查询积累后再开自动优化。

---

## 边界声明（防过度开发 · 对应 scope-guardian）

- **MVP = Phase 0-2**：一个**中文能真搜到东西**的 BM25 检索。这就已经比 ui-ux-pro-max 直接套上来强一个量级（它在中文上是坏的）。
- **真正的差异化 = Phase 3-4**：图谱 + 方向一致性提示。这是「校验」区别于纯搜索的地方。
- **明确不做**：向量/embedding（非确定性，毁掉校验语义）、LLM 重排序（加成本+不确定性到一个必须确定的层）、Phase 6 自动调优（无真实数据）。
- 每加一层先问：I 层是确定性硬校验，这层会不会让它变不确定？会 → 不加。

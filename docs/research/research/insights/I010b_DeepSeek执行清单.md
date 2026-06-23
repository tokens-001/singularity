# I010b — DeepSeek 执行清单（配合 I010 审计报告）

> 用途：I010 报告里"必须先改"的4条硬伤，拆成可直接执行的步骤。
> 给 DeepSeek：照下面逐条改，每条改完跑"验证"那一行确认。不需要重新判断要不要改——Opus 审计已定。
> 改任何文件前先读当前文件确认行号未漂移（行号基于 2026-06-14 状态）。

---

## 修复 1：app.py 缺 import re（致命Bug，最优先）

**位置**：`python/app.py:4`
**现状**：
```python
import os, json, uuid
```
`re.findall` 在 `app.py:90` 和 `app.py:120` 被调用，但 `re` 从未导入。`/related/<fname>`（:411）和 `/case/<fname>`（:374）一旦触发就抛 `NameError: name 're' is not defined`。

**改成**：
```python
import os, json, uuid, re
```

**验证**：
```
cd python && python -c "import ast,sys; ast.parse(open('app.py').read()); print('ok')"
grep -n 'import re\|re\.findall' app.py   # 确认 import re 在第4行、findall在90/120
```
启动服务后访问任意 `/related/xxx`，应返回 JSON（空数组也行），不再 500。

---

## 修复 2：把3个伪校验项移出"硬校验层"话术（定位级诚实问题）

这3项（论证链检测/证据缺失检测/相反法条发现）本质是再调一次 LLM 让它自己说"可能有问题"，不是硬校验。它们可以保留为分析功能，但**不能挂在"硬校验"卖点下**，否则对外定位与代码事实冲突。

**改动 2a — 首页标语**
- 位置：`python/templates/index.html:417`
- 现状：`<div class="tagline">AI驱动的判例分析 · 硬校验层保障</div>`
- 改成：`<div class="tagline">AI驱动的判例分析 · 溯源校验 + 法条版本核对</div>`
- 理由：只保留真正的硬校验（溯源越界、法条版本）入标语，不用"硬校验层"这种把伪校验也圈进去的总称。

**改动 2b — 三张卡片的兜底文案（当前在"撒谎"）**
- 位置：`index.html:828-830`
- 现状：
```javascript
document.getElementById("r-argchain").textContent = a.论证链检测 || "已检测，未发现明显逻辑断裂";
document.getElementById("r-evidence").textContent = a.证据缺失检测 || "已检测，未发现明显证据缺失";
document.getElementById("r-counterlaw").textContent = a.相反法条发现 || "已检索，未发现明显冲突法条";
```
- 问题：LLM 返回空时，UI 自动显示"已检测，未发现明显X"——这是在没有任何校验发生时声称"已检测通过"。
- 改成（兜底文案改为中性、不声称做过校验）：
```javascript
document.getElementById("r-argchain").textContent = a.论证链检测 || "本项为AI辅助分析，本次无输出";
document.getElementById("r-evidence").textContent = a.证据缺失检测 || "本项为AI辅助分析，本次无输出";
document.getElementById("r-counterlaw").textContent = a.相反法条发现 || "本项为AI辅助分析，本次无输出";
```

**改动 2c — 三张卡片标题加"AI分析"标识，与硬校验区分**
- 位置：`index.html:486-488`
- 现状：`🔗 论证链检测` / `🔎 证据缺失检测` / `⚖️ 相反法条发现`
- 改成：`🔗 论证链分析(AI)` / `🔎 证据缺失分析(AI)` / `⚖️ 相反法条提示(AI)`
- 同步把同名词在反馈选项里更新：`index.html:557-559、576-578、595-597` 三组 `论证链检测/证据缺失检测/相反法条发现` 的显示文字同步改为带"(AI)"的版本（value 值可不改，避免动后端键名；只改 label 显示文字）。

**验证**：页面跑一遍分析，三张卡标题带"(AI)"，标语不再出现"硬校验层"四个字。`grep -n "硬校验" python/templates/index.html` 应无结果（或仅剩注释）。

---

## 修复 3：移除3个空壳UI入口（展示永远空的功能=伪可信）

法条库目前只有1个文件（`data/laws/民法典-侵权责任编.txt`，5条且跳号），下面三项在真实数据下永远是空或崩。在法条库扩到有意义规模前，从UI隐藏。

**3a — 相邻法条发现**
- UI 块：`index.html:497-499` 的 `<!-- 相邻法条发现 -->` 整块容器
- 渲染逻辑：`index.html:836-841`（`if (data.相邻法条 && data.相邻法条.length > 0)`）
- 后端：`app.py:312` `相邻法条 = find_adjacent_laws(...)`、:335 返回 `"相邻法条": 相邻法条`
- 做法（最小改动、可逆）：把 `app.py:312` 改为 `相邻法条 = []`，保留函数 `find_adjacent_laws` 不删（法条库扩充后改回一行即恢复）。UI 因 `length > 0` 判断会自动不渲染，无需删HTML。
- 注释标注：在 :312 上方加 `# 法条库数据不足，暂禁用相邻法条（扩库后改回 find_adjacent_laws(法条库目录, 法条对照)）`

**3b — 关联判例**
- UI 块：`index.html:521-523`
- 后端：`app.py:406` `"关联判例": 查关联判例(fname)`、`/related` 路由 :411-414
- 做法：先完成"修复1"（import re）让它不崩；然后因为真实判例数据稀少，同样在 `app.py:406` 改为 `"关联判例": []`，加注释 `# 判例存量不足，暂禁用关联（积累后改回 查关联判例(fname)）`。`/related` 路由保留。
- 注意：修复1 仍要做——否则 `构建判例索引` 在别处被调到还是会崩。

**3c — 外部案例库检索（仅存在于旧版 case_analyzer.py）**
- 位置：`case_analyzer.py:141-152` `搜索案例库` + 菜单项
- 这个随"修复4删 case_analyzer.py"一起消失，此处不单独处理。

**验证**：跑一份判例，页面不再出现"相邻法条""关联判例"两个区块（因数据为空自动隐藏）。`/related/任意` 不 500。

---

## 修复 4：可信度评分诚实化 + 删冗余副本

**4a — 可信度评分降级为三档+明细，停止伪精确**
- 位置：`python/skills/legal/score_analysis.py:64-113`（`compute_trust_score`）
- 问题：起点 `基础分=70`（:67）+ 全硬编码魔数，输入是1个文件的空库，输出却是0-100精确分。
- 最小诚实化做法（**不改算法，只改对外呈现**）：
  - 后端 `score_analysis.py` 返回值已含 `等级`（高/中/低）和 `明细`，保留。
  - 前端 `index.html:804-806` 可信度仪表盘：**不再显示具体数字分**，只显示`等级`（高/中/低）+ `明细`列表。把数字盘面（0-100进度环）换成三档色块或直接隐藏数字。
  - 理由：明细（"溯源全部验证 +10""无法条库对照 -10"）是真实信号、有价值，保留；那个被伪精确包装的总分数字撤掉。
- 若要更彻底（可选，非必须）：把 `基础分=70` 注释说明"70为经验起点、无校准依据"，提醒后续接入真实信号后重算。

**4b — 删除 case_analyzer.py 冗余副本**
- 位置：整个 `python/case_analyzer.py`（364行）
- 现状：skills化之前的旧CLI全量副本，与 app.py 体系平行冗余，含空壳"搜索案例库"。
- 做法：先确认无任何文件 import 它——
```
cd python && grep -rn "case_analyzer\|import case_analyzer\|from case_analyzer" . --include=*.py | grep -v case_analyzer.py
```
  无结果则直接 `git rm case_analyzer.py`。有结果则先报给用户，别删。

**4c — 10个技能模块的重复 `法条提示` 抽到 _base.py**
- 现状：`法条提示 = "【硬性要求】必须引用...至少引用2条..."` 这段在10个技能文件各抄一份。
- 做法：在 `python/skills/legal/_base.py` 加一行 `法条提示 = "..."`（用现有文案），10个技能文件改为 `from ._base import 问AI, 法条提示`，删除各自的本地 `法条提示` 定义。
- 涉及文件：`audit_argument_chain / detect_missing_evidence / discover_opposing_laws / extract_dispute / extract_reasoning / find_unanswered / assess_transfer / find_counter_arguments / generate_summary / structure_judgment`（逐个确认是否含 `法条提示`，含才改）。

**验证**：
```
cd python && python -c "import ast; [ast.parse(open(f).read()) for f in __import__('glob').glob('skills/legal/*.py')]; print('parse ok')"
grep -rn "^法条提示 = " skills/legal/*.py   # 改完应只剩 _base.py 一处
```

---

## 执行顺序

1. **修复1**（import re）—— 一行，先做，解除崩溃。
2. **修复4b**（删 case_analyzer.py）—— 确认无引用后删，清掉一大块干扰。
3. **修复3**（禁用3个空壳，各改一行+注释）。
4. **修复2**（标语/卡片/兜底文案 去"硬校验"话术）。
5. **修复4a**（评分撤数字、留等级+明细）。
6. **修复4c**（抽 `法条提示`，纯重构，最后做）。

每完成一步，跑该步的"验证"行。全部完成后 `git add -A && git commit`，commit message 写明"按I010审计修复硬校验话术与空壳功能"。

> 知识体系层的修改（断链清理、结论收口、P系列诚实化）见 I010 报告"其次"和"可以等"，不在本清单——本清单只覆盖"必须先改"的代码4条。

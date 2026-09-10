"""提取员召回率：同一个议程，换个模型来提，会漏掉多少？

背景：v2 融合的第②步「提取三类」是**单模型单次调用**，它列出的 disagreements
就是后面所有辩论轮次能谈的全部议题（`execution_judge.py:659-718` 整段遍历它）。
清单上没有的分歧 → 没有一轮碰得到 → 定稿人拿到的 resolved 里也没有它。

方法：同一批稿子喂 K 个不同提取员（都在委员会之外，避免选手出题），
再用一个裁判把这 K 份清单**对齐成规范集合**，标记每条被谁提到了。

  recall(m)    = m 找到的真分歧 / 全体提取员找到的真分歧并集
  precision(m) = m 列出的真分歧 / m 列出的条目

recall 低 = 谁当提取员谁漏，机制在赌单点。recall 高 = 议程稳定，不用改。

用法: .venv/bin/python tests/integration/extract_recall.py [brief号... 默认全部]
      AB_EXTRACTORS=glm-5.3-flash,glm-5.2,deepseek-v4-pro
"""
import os, sys, json, importlib.util
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
import singularity.scheduler.dispatcher  # noqa: F401
from singularity.scheduler import config as _cfg
from singularity.scheduler import execution_judge as ej

HERE = Path(__file__).resolve().parent
CACHE = HERE / ".extract_recall_cache.json"
# 默认提取员 = 生产默认 + 两个同价位备选。**都要在委员会之外**（选手给自己出题
# 会系统性偏向自己的方案，实测 warn:fusion_self_judge 就是防这个）。
EXTRACTORS = [m.strip() for m in
              os.environ.get("AB_EXTRACTORS", "glm-5.3-flash,glm-5.2,deepseek-v4-pro").split(",")]
JUDGE = os.environ.get("AB_JUDGE", "deepseek-v4-pro")

ALIGN = """下面是同一个架构需求的 {n} 份方案，以及 3 个不同的「秘书」各自提取的分歧清单。
它们看的是同一批方案，但列出的条目不一样。

【原始需求】
{task}

【各秘书的分歧清单】
{lists}

请把这几份清单**对齐成一份规范集合**：同一处分歧只算一条（说法不同但指同一处的合并），
并对每条给出：
  - point:  一句话说明这处分歧是什么（哪一处、两种不能并存的解法分别是什么）
  - found_by: 哪些秘书列出了它（用秘书名）
  - is_real: 它是不是**真正互斥**的分歧（两种解法不能同时存在于一份方案里）。
             只是详略不同 / 措辞不同 / 一方没提到但不冲突 → false

注意：is_real=false 的也要列出来（它在某份清单上出现过），别丢，用字段区分。
只输出 JSON：
{{"items": [{{"point": "...", "found_by": ["秘书名"], "is_real": true}}]}}"""


def load_cache() -> dict:
    return json.loads(CACHE.read_text()) if CACHE.exists() else {}


def extract(brief_no: int, task: str, plans: list, model: str, cache: dict) -> list:
    """一个提取员的清单。命中缓存不重复烧钱。"""
    key = f"{brief_no}|{model}"
    if key in cache:
        return cache[key]
    # 走生产的 prompt 和 _plans_block：**测试必须和生产同形**，
    # 裸 brief 会得到和生产不一样的提取结果（AB 实验为此翻过一次车）。
    outputs = ej._plans_block([(m, p) for m, p in plans])
    raw = ej._call_model(ej._V2_EXTRACT.format(n=len(plans), task=task, outputs=outputs),
                         model, max_tokens=ej._V2_EXTRACT_MAX_TOKENS)
    d = ej.try_parse_json(raw) if raw else None
    if not isinstance(d, dict) or d.get("parse_error"):
        print(f"    {model}: 提取失败（raw {len(raw or '')} 字）", flush=True)
        items = []
    else:
        items = [x for x in (d.get("disagreements") or []) if isinstance(x, dict)]
        print(f"    {model}: {len(items)} 条分歧"
              + ("".join(f"\n        · {i.get('dimension')}: {i.get('point')}" for i in items)), flush=True)
    cache[key] = items
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2))
    return items


def align(brief_no: int, task: str, per_model: dict, cache: dict) -> list:
    key = f"{brief_no}|__aligned__|{','.join(per_model)}"
    if key in cache:
        return cache[key]
    lists = "\n\n".join(
        f"### 秘书 {m}（{len(v)} 条）\n" + (ej._j(v) if v else "（空）")
        for m, v in per_model.items())
    raw = ej._call_model(ALIGN.format(n=2, task=task, lists=lists), JUDGE,
                         max_tokens=_cfg.MODEL_MAX_TOKENS)
    d = ej.try_parse_json(raw) if raw else None
    items = d.get("items") if isinstance(d, dict) else None
    if not isinstance(items, list):
        print(f"  ⚠ 对齐失败（raw {len(raw or '')} 字），跳过该 brief", flush=True)
        return []
    cache[key] = items
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2))
    return items


def main():
    spec = importlib.util.spec_from_file_location("ab_fusion", HERE / "ab_fusion.py")
    ab = importlib.util.module_from_spec(spec); spec.loader.exec_module(ab)
    store = json.loads((HERE / ".v2_plans.json").read_text())
    briefs = [int(x) for x in sys.argv[1:]] or [int(k) for k in store]

    cache = load_cache()
    total = {m: [0, 0] for m in EXTRACTORS}       # 模型 → [找到的真分歧, 列出的条目]
    for no in briefs:
        task = ab.arch_task(ab.BRIEFS[no - 1])     # 生产形态（带角色提示词 + schema）
        plans = store[str(no)]
        print(f"\n=== brief {no} | 提取员 {EXTRACTORS} ===", flush=True)
        per_model = {m: extract(no, task, plans, m, cache) for m in EXTRACTORS}
        merged = align(no, task, per_model, cache)
        real = [x for x in merged if x.get("is_real")]
        print(f"  对齐后：真分歧 {len(real)} 条，"
              f"其中 {len(merged) - len(real)} 条被判为「不算真分歧」")
        for m in EXTRACTORS:
            # found_by 是裁判对齐后的归属，比拿 point 字符串比对可靠
            got = sum(1 for x in real if m in (x.get("found_by") or []))
            listed = sum(1 for x in merged if m in (x.get("found_by") or []))
            total[m][0] += got
            total[m][1] += listed
            print(f"    {m:<20} 召回 {got}/{len(real)}"
                  + (f" = {got/len(real):.0%}" if real else "")
                  + f" | 提出 {listed} 条"
                  + (f"（{listed-got} 条被判非真分歧）" if listed > got else ""))

    print(f"\n{'提取员':<22}{'找到真分歧':<12}{'提出条数':<10}{'精确率'}")
    for m, (got, listed) in total.items():
        prec = f"{got/listed:.0%}" if listed else "—"
        print(f"  {m:<20}{got:<12}{listed:<10}{prec}")
    print("\n注：召回低 = 换谁当提取员议程就变，机制在赌单点；"
          "全 100% = 议程稳定，不用改。")


if __name__ == "__main__":
    main()

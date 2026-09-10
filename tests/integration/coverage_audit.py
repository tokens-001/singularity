"""覆盖审计：两份单稿的缺失项是相交还是互补？

这是 selection vs fusion 的前提问题（glm-5.3 建议，2026-09-10）：
  - 缺失项大面积**不相交** → 两个模型盲区互补 → 融合有收益空间，值这个价
  - 缺失项**高度重合** → 两个模型犯一样的错 → 融合是花钱买平均，不如「跑两份+选优」

方法：**独立审计**，一次只看一份稿（位置偏好从结构上消失）。
所有稿子用同一份需求条目清单，缺失项才可比。

用法: .venv/bin/python tests/integration/coverage_audit.py [brief号 默认3]
      AB_AUDITOR=deepseek-v4-pro
"""
import os, sys, json, importlib.util
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
import singularity.scheduler.dispatcher  # noqa: F401
from singularity.scheduler import config as _cfg
from singularity.scheduler import execution_judge as ej

HERE = Path(__file__).resolve().parent
BRIEF_NO = int(sys.argv[1]) if len(sys.argv) > 1 else 3
AUDITOR = os.environ.get("AB_AUDITOR", "deepseek-v4-pro")

# 需求条目清单：从 brief 拆出的可勾选条目。**两份稿子必须用同一份**，
# 否则各自列出自己的检查项，缺失集合没法比。
REQUIREMENTS = {
    3: ["多租户架构", "按用量计费", "套餐支持", "超额处理", "退款",
        "账单生成", "对账", "租户隔离", "模块划分", "数据模型", "API契约", "任务拆解"],
}

PROMPT = """下面是需求条目清单和一份架构方案。

【第一部分】逐条判断方案是否覆盖了该条目：
{reqs}
每条给 covered（有明确设计）/ partial（提到了但不完整）/ missing（找不到）。

【第二部分】列出方案里**需求没要求**的模块 / 数据表 / 接口（范围违例）。
只列真正多出来的东西（如需求只说计费，方案却引入支付网关、账本、缓存）。
没有就返回空列表。

架构方案:
{plan}

只输出 JSON: {{"audit": [{{"id": 1, "status": "covered|partial|missing", "note": "一句话"}}],
               "out_of_scope": ["违例：模块名 —— 为什么算超出范围"]}}"""


def derive_requirements(brief: str) -> list[str]:
    """从需求拆出可逐条勾选的检查项 —— 「需求覆盖表」的尺子。

    所有稿子必须用同一份清单，否则各列各的、缺失集合没法比。
    拆的时候要细到**可判定有无**，否则会拆出"模块划分""数据模型"这种
    任何像样方案都会 covered 的空条目（实测：12 条全 covered，测不出差异）。
    """
    prompt = f"""下面是需求，请拆成可逐条判定的需求条目（12-20 条）。

要求：每条必须是**能判定"方案里有没有"的具体点**，例如
"金额用什么类型表示""并发退款如何防止超退""跨租户访问是否防止枚举"。
不要拆成"模块划分""数据模型"这类任何方案都会覆盖的空条目。

需求:
{brief}

只输出 JSON: {{"requirements": ["条目1", "条目2", ...]}}"""
    raw = ej._call_model(prompt, AUDITOR, max_tokens=_cfg.MODEL_MAX_TOKENS)
    d = ej.try_parse_json(raw) if raw else None
    if isinstance(d, dict) and isinstance(d.get("requirements"), list):
        return [str(x) for x in d["requirements"]][:30]
    return []


def record_discipline(results: dict) -> None:
    """把这次审计的范围违例数累积进模型纪律表 —— _pick_writer 靠它选定稿人。

    只记选手（融合稿是产物，不是模型的纪律）。
    """
    try:
        p = _cfg.QIDIAN_DIR / "model_discipline.json"
        disc = json.loads(p.read_text()) if p.exists() else {}
        for name, r in results.items():
            if name == "融合稿":
                continue
            d = disc.setdefault(name, {"violations": 0, "audits": 0})
            d["violations"] += len(r.get("_out_of_scope", []))
            d["audits"] += 1
        p.write_text(json.dumps(disc, ensure_ascii=False, indent=2))
        print(f"\n已更新模型纪律表 → {p}")
        for m, d in sorted(disc.items()):
            print(f"  {m}: 违例 {d['violations']} / 审计 {d['audits']} 次"
                  f" = {d['violations']/max(d['audits'],1):.1f} 处每次")
    except Exception as e:
        print(f"\n纪律表写入失败: {e}")


def gate(oos: list[str], limit: int = 3) -> tuple[bool, str]:
    """范围违例超阈值 → 拦。返回 (通过?, 理由)。

    这是「把范围约束从 prompt 升级成硬门禁」的判定部分：prompt 版的约束靠定稿人
    自觉，实测把 payment 从 17 压到 1，但 ledger 那类"有争议的"压不下去（14→23）。
    """
    if len(oos) <= limit:
        return True, ""
    return False, f"范围违例 {len(oos)} 处 > 阈值 {limit}：" + "；".join(oos[:2])


def audit(name: str, text: str, reqs: list[str]) -> dict:
    req_block = "\n".join(f"{i+1}. {r}" for i, r in enumerate(reqs))
    prompt = PROMPT.replace("{reqs}", req_block).replace("{plan}", text)
    raw = ej._call_model(prompt, AUDITOR, max_tokens=_cfg.MODEL_MAX_TOKENS)
    d = ej.try_parse_json(raw) if raw else None
    if not isinstance(d, dict) or not isinstance(d.get("audit"), list):
        print(f"  {name}: 审计失败（返回 {len(raw or '')} 字）", flush=True)
        return {}
    out = {}
    for item in d["audit"]:
        try:
            out[reqs[int(item["id"]) - 1]] = item.get("status", "?")
        except (KeyError, IndexError, ValueError, TypeError):
            continue
    oos = [str(x)[:60] for x in (d.get("out_of_scope") or [])]
    out["_out_of_scope"] = oos            # 特殊键，不参与覆盖率统计
    print(f"  {name}: 审计完成 {len(out)-1}/{len(reqs)} 条 | 范围违例 {len(oos)} 处", flush=True)
    return out


def main():
    # AB_REQS=auto 用模型从 brief 现拆（生产路径）；否则用手工清单（brief 3 用）
    if os.environ.get("AB_REQS") == "auto":
        spec_ = importlib.util.spec_from_file_location("ab_fusion", HERE / "ab_fusion.py")
        _ab = importlib.util.module_from_spec(spec_); spec_.loader.exec_module(_ab)
        print("拆需求条目中…", flush=True)
        reqs = derive_requirements(_ab.BRIEFS[BRIEF_NO - 1])
        if not reqs:
            print("❌ 拆需求失败"); return
        print(f"拆出 {len(reqs)} 条：")
        for i, r in enumerate(reqs, 1):
            print(f"  {i}. {r}")
        print()
    else:
        reqs = REQUIREMENTS[BRIEF_NO]
    store = json.loads((HERE / ".v2_plans.json").read_text())[str(BRIEF_NO)]
    docs = {m: p for m, p in store}
    f = Path(f"/tmp/v2_fused_{BRIEF_NO}.json")
    if f.exists():
        docs["融合稿"] = f.read_text()

    print(f"brief {BRIEF_NO} | 审计员 {AUDITOR} | 需求 {len(reqs)} 条 | 稿子 {list(docs)}\n")
    results = {name: audit(name, text, reqs) for name, text in docs.items()}

    print(f"\n{'需求条目':<14}" + "".join(f"{n:<22}" for n in docs))
    for r in reqs:
        row = f"{r:<14}"
        for n in docs:
            row += f"{results[n].get(r, '?'):<22}"
        print(row)

    # ── 核心：两份单稿的缺失项是相交还是互补 ──
    singles = [n for n in docs if n != "融合稿"]
    if len(singles) == 2:
        a, b = singles
        miss_a = {r for r in reqs if results[a].get(r) in ("missing", "partial")}
        miss_b = {r for r in reqs if results[b].get(r) in ("missing", "partial")}
        inter, union = miss_a & miss_b, miss_a | miss_b
        print(f"\n── 两份单稿的缺失项（partial 也算）──")
        print(f"  {a} 缺: {sorted(miss_a) or '无'}")
        print(f"  {b} 缺: {sorted(miss_b) or '无'}")
        print(f"  共同缺（重合）: {sorted(inter) or '无'}")
        print(f"  各自独缺（互补）: {sorted(union - inter) or '无'}")
        if union:
            print(f"\n  ▶ 重合率 {len(inter)}/{len(union)} = {len(inter)/len(union):.0%}"
                  f"  → " + ("盲区高度重合，融合≈花钱买平均，selection 更划算"
                             if len(inter) / len(union) > 0.5 else
                             "盲区互补，融合有收益空间"))
        else:
            print("\n  ▶ 两份都没缺 —— 无从判断互补性（换个更难的 brief）")

    # ── 范围违例：brief 3 输掉那场就栽在这个维度（覆盖率全满也没用）──
    limit = int(os.environ.get("AB_OOS_LIMIT", "3"))
    print(f"\n── 范围违例（需求没要求却写进方案的）｜门禁阈值 {limit} 处 ──")
    for n in docs:
        items = results[n].get("_out_of_scope", [])
        ok, reason = gate(items, limit)
        print(f"  {n}: {len(items)} 处 → " + (f"通过" if ok else f"**拦下**（{reason}）"))
        for x in items[:6]:
            print(f"      · {x}")

    record_discipline(results)


if __name__ == "__main__":
    main()

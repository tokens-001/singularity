"""转正验证：不设任何环境变量，走生产的 `_dispatch_committee`，确认默认真的跑 v2。

转正换掉的是现役默认，所以不能只靠单测打桩 —— 得真跑一次，确认
① 默认判定为开 ② 真的走了 v2（没有 warn:fusion_v2_failed_fallback_legacy）
③ 产物是完整 JSON（没被截断成半截）

用法: .venv/bin/python tests/integration/v2_default_probe.py
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
import singularity.scheduler.dispatcher as disp          # noqa: F401  破循环依赖
from singularity.scheduler import _dispatch_exec as de, execution_judge as ej, witness

os.environ.pop("QIDIAN_FUSION_V2", None)                 # 用默认值，别显式开

TASK = ("设计一个短链接服务，覆盖：模块划分、数据模型、API 契约、任务拆解。"
        "要考虑高并发跳转和过期清理。")
MEMBERS = ["glm-5.3-flash", "deepseek-v4-flash"]


def main():
    print(f"默认 v2 启用? {ej._fusion_v2_enabled()}（应为 True）")
    warns = []
    orig = witness.warn
    witness.warn = lambda scope, msg: (warns.append(str(msg)), orig(scope, msg))[1]
    try:
        t0 = time.time()
        r = de._dispatch_committee(TASK, "any", "probe_v2_default",
                                   {}, [{"model": m} for m in MEMBERS])
        dt = time.time() - t0
    finally:
        witness.warn = orig

    raw = r.executor_result.raw_output if r and r.executor_result else ""
    fm = getattr(getattr(r, "executor_result", None), "fusion_meta", None)
    fell_back = [w for w in warns if "fallback_legacy" in w]
    failed = [w for w in warns if w.startswith(("debate_failed", "fusion_failed",
                                                "fusion_empty"))]
    # 原文必须落盘 —— 判据可能撒谎（产出常带 ``` 围栏就不以 } 结尾），
    # 这个仓库最贵的一课就是"数字/判据反常时先读原始产物"。
    out = Path("/tmp/v2probe_out.txt")
    out.write_text(raw, encoding="utf-8")
    print(f"耗时 {dt:.0f}s | 产出 {len(raw)} 字 | 成员 {fm['models'] if fm else '（无 fusion_meta）'}")
    print(f"回退旧流程? {'是 → ' + fell_back[0] if fell_back else '否（走的是 v2）'}")
    if failed:
        print(f"降级告警: {failed}")
    print(f"原文已落盘 → {out}")
    print("=== 尾部 300 字 ===")
    print(repr(raw[-300:]))

    # 真判据：能被解析出架构 JSON（fence 由 try_parse_json 处理）
    from singularity.scheduler._io import try_parse_json
    parsed = try_parse_json(raw, try_repair=True) if raw else {}
    complete = isinstance(parsed, dict) and bool(parsed.get("modules")) \
        and bool(parsed.get("tasks"))
    print(f"能解析出 modules+tasks? {'是' if complete else '否'} "
          f"(keys={sorted(parsed)[:8] if isinstance(parsed, dict) else type(parsed).__name__})")
    ok = bool(fm) and not fell_back and not failed and complete
    print("\n" + ("✅ 转正路径跑通" if ok else "❌ 有问题，见上"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

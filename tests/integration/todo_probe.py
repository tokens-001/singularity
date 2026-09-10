"""黑盒跑一遍 todo.py：机械记录「有没有做到」，不做主观打分。

用法: .venv/bin/python tests/integration/todo_probe.py /tmp/role_implementer.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

TARGET = Path(sys.argv[1]).resolve()


def run(args, home, stdin=""):
    r = subprocess.run([sys.executable, str(TARGET), *args], cwd=home,
                       input=stdin, capture_output=True, text=True, timeout=60,
                       env={**os.environ, "HOME": home, "NO_COLOR": "1"})
    return r.returncode, r.stdout + r.stderr


def main():
    home = tempfile.mkdtemp(prefix="todo-probe-")
    store = Path(home) / ".todo.json"
    checks = []

    def ck(name, ok, note=""):
        checks.append((name, ok, note))

    rc, out = run(["add", "买牛奶"], home)
    ck("add 退出码 0", rc == 0, f"rc={rc}")
    ck("add 后落盘", store.exists())
    ck("落盘是合法 JSON", store.exists() and _valid_json(store))
    first = _pid(store) if store.exists() else None

    rc, out = run(["add", "写周报"], home)
    ck("add 第二条", store.exists() and len(_items(store) or []) == 2, f"len={len(_items(store) or [])}")

    rc, out = run(["list"], home)
    ck("list 退出码 0", rc == 0, f"rc={rc}")
    ck("list 显示两条内容", "买牛奶" in out and "写周报" in out)
    ck("list 带序号", any(s in out for s in ("1.", "1)", "[1]", "1 ")), repr(out[:80]))

    rc, out = run(["done", "1"], home)
    ck("done 退出码 0", rc == 0, f"rc={rc}")
    items = _items(store) or []
    ck("done 真的改了状态", any(i.get("done") is True or i.get("completed") is True or i.get("status") == "done"
                                for i in items), json.dumps(items, ensure_ascii=False)[:120])

    rc, out = run(["list"], home)
    ck("list 区分勾选态", ("[x]" in out or "✓" in out.lower() or "✔" in out) and
       ("[ ]" in out or "○" in out) or "√" in out, repr(out[:80]))

    rc, out = run(["remove", "2"], home)
    ck("remove 退出码 0", rc == 0, f"rc={rc}")
    ck("remove 真的删了", len(_items(store) or []) == 1,
       json.dumps(_items(store), ensure_ascii=False)[:120])

    # 关键边缘路径：损坏文件不能崩
    store.write_text("{ 这不是 JSON", encoding="utf-8")
    rc, out = run(["list"], home)
    ck("损坏文件不崩(rc∈{0,1,2} 且无 Traceback)", rc in (0, 1, 2) and "Traceback" not in out,
       f"rc={rc} tail={out.strip().splitlines()[-1] if out.strip() else ''!r}")
    ck("损坏文件有可读提示", any(k in out for k in ("损坏", "无法", "corrupt", "JSON", "解析", "格式")),
       repr(out[:120]))
    bak = list(Path(home).glob("*.bak")) + list(Path(home).rglob("*.bak"))
    ck("损坏文件被备份(加分项)", bool(bak), str([b.name for b in bak]))

    rc, out = run(["done", "abc"], home)
    ck("非法序号不崩", rc in (0, 1, 2) and "Traceback" not in out, f"rc={rc}")

    rc, out = run([], home)
    ck("无参数不崩", "Traceback" not in out, f"rc={rc}")

    rc, out = run(["--selftest"], home)
    ck("自带 --selftest(加分项)", rc == 0 and "Traceback" not in out, f"rc={rc}")

    ok = sum(1 for _, o, _ in checks if o)
    print(f"{TARGET}  →  {ok}/{len(checks)}\n")
    for name, o, note in checks:
        print(f"  {'✅' if o else '❌'} {name}" + (f"   [{note}]" if not o else ""))
    return 0 if ok == len(checks) else 1


def _valid_json(p):
    try:
        json.loads(p.read_text(encoding="utf-8"))
        return True
    except Exception:
        return False


def _items(p):
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    return d if isinstance(d, list) else d.get("items") or d.get("todos")


def _pid(p):
    return None


if __name__ == "__main__":
    sys.exit(main())

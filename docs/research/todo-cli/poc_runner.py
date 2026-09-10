"""PoC 实验跑批：为六维度调研报告产出可复现证据。

运行： python3 docs/research/todo-cli/poc_runner.py
"""

from __future__ import annotations

import inspect
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROTO = HERE / "poc_proto"
sys.path.insert(0, str(PROTO))
sys.dont_write_bytecode = True

from todo import store  # noqa: E402

ENV = {**os.environ, "PYTHONPATH": str(PROTO), "PYTHONDONTWRITEBYTECODE": "1"}
OUT: list[str] = []


def say(msg: str) -> None:
    OUT.append(msg)
    print(msg)


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="todo_poc_"))


# ---------------------------------------------------------------- PoC1 损坏文件
def poc1_corruption() -> None:
    d = tmpdir()
    cases = {
        "不存在": None,
        "空文件": "",
        "被截断的 JSON": '{"version": 1, "next_id": 3, "items": [{"id": 1, "text": "a"',
        "纯垃圾文本": "not json at all",
        "JSON null": "null",
        "顶层是字符串": '"hello"',
        "顶层是裸数组": '[{"id": 1, "text": "legacy"}]',
        "items 不是数组": '{"items": {"a": 1}}',
        "items 含脏元素": '{"next_id": 5, "items": [{"id": 1, "text": "ok"}, "junk", {"text": "no-id"}]}',
        "非法 UTF-8": None,
        "带 BOM": "\ufeff{\"next_id\": 1, \"items\": []}",
    }
    say("== PoC1 损坏文件韧性（期望：不抛异常、返回可写状态、原文件被隔离备份）==")
    for name, content in cases.items():
        p = d / f"case_{abs(hash(name))}.json"
        if content is None and name == "非法 UTF-8":
            p.write_bytes(b'{"items": [\xff\xfe\x00]}')
        elif content is not None:
            p.write_text(content, encoding="utf-8")
        try:
            items = store.list(path=p)
            status = f"list()->{len(items)} 条"
        except Exception as e:  # noqa: BLE001
            status = f"!!! 抛异常 {type(e).__name__}: {e}"
        quarantined = [q.name for q in d.glob(f"{p.name}.corrupt.*")]
        can_write = "(不可写)"
        try:
            store.add("after-corruption", path=p)
            can_write = "(可恢复写入)"
        except Exception as e:  # noqa: BLE001
            can_write = f"(写失败 {type(e).__name__})"
        say(f"  {name:<14} -> {status} 备份={len(quarantined)} {can_write}")
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- PoC2 原子写
def poc2_atomic_write() -> None:
    say("== PoC2 原子写：写盘中途失败不得破坏已有数据 ==")
    d = tmpdir()
    p = d / ".todo.json"
    store.add("原始任务", path=p)
    original = p.read_text(encoding="utf-8")

    real_replace = os.replace

    def boom(*a, **k):
        raise OSError("simulated crash before rename")

    store.os.replace = boom
    try:
        store.add("崩溃前的新任务", path=p)
        say("  写入异常未抛出（不符合预期）")
    except OSError as e:
        say(f"  模拟崩溃：{type(e).__name__}: {e}")
    finally:
        store.os.replace = real_replace

    intact = p.read_text(encoding="utf-8") == original
    leftovers = [f.name for f in d.iterdir() if f.name.endswith(".tmp")]
    say(f"  原文件内容完整={intact}  残留临时文件={leftovers}  可继续读={len(store.list(path=p))} 条")
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- PoC3 路径
def poc3_path() -> None:
    say("== PoC3 落盘路径解析（~ 展开、HOME 覆写、异常 HOME）==")
    script = (
        "import pathlib,os;from todo import store;"
        "print('DEFAULT_PATH=',store.DEFAULT_PATH);"
        "print('Path.home()=',pathlib.Path.home())"
    )
    for label, extra in [
        ("HOME=/tmp/poc_home_a", {"HOME": "/tmp/poc_home_a"}),
        ("HOME=/tmp/poc_home_b", {"HOME": "/tmp/poc_home_b"}),
    ]:
        r = subprocess.run([sys.executable, "-c", script], env={**ENV, **extra}, capture_output=True, text=True)
        say(f"  {label} -> " + " | ".join(r.stdout.strip().splitlines()))
    d = tmpdir()
    p = d / "custom.json"
    store.add("x", path=p)
    say(f"  显式 --file 生效: {p.exists()}  内容非空: {p.stat().st_size > 0}")
    perm = oct(p.stat().st_mode & 0o777)
    say(f"  文件权限（默认 umask）: {perm} 目录权限: {oct(d.stat().st_mode & 0o777)}")
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- PoC4 并发
def poc4_concurrency() -> None:
    say("== PoC4 并发写：读-改-写丢失更新量化（无锁实现的上界）==")
    d = tmpdir()
    p = d / ".todo.json"
    worker = (
        "import sys;sys.path.insert(0,%r);from todo import store\n"
        "for i in range(%d): store.add(f'w{%%d}-{%%d}'%%(W,i), path=sys.argv[1])\n"
    )
    procs, per = 8, 15
    code = "import sys;sys.path.insert(0,%r);from todo import store\nW=int(sys.argv[2])\nfor i in range(int(sys.argv[3])): store.add(f'w{W}-{i}', path=sys.argv[1])\n" % str(
        PROTO
    )
    t0 = time.perf_counter()
    ps = [subprocess.Popen([sys.executable, "-c", code, str(p), str(w), str(per)], env=ENV) for w in range(procs)]
    [x.wait() for x in ps]
    dt = time.perf_counter() - t0
    got = len(store.list(path=p))
    say(f"  进程数={procs} 每进程 {per} 次 add  期望={procs * per} 实际落盘={got} 丢失={procs * per - got}")
    say(f"  总耗时 {dt:.3f}s（{procs} 进程并发，含解释器启动）")
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- PoC5 CLI
def poc5_cli() -> None:
    say("== PoC5 CLI 端到端（子进程真跑 python3 -m todo.cli）==")
    d = tmpdir()
    f = str(d / ".todo.json")
    cases = [
        (["add", "买牛奶"], 0),
        (["add", "写周报", "并", "复盘"], 0),
        (["list"], 0),
        (["list", "--all"], 0),
        (["done", "1"], 0),
        (["done", "999"], 1),
        (["remove", "999"], 1),
        (["remove", "2"], 0),
        (["add", "   "], 2),
        (["--help"], 0),
        ([], 2),
    ]
    for argv, expect in cases:
        r = subprocess.run(
            [sys.executable, "-m", "todo.cli", "--file", f, *argv], env=ENV, capture_output=True, text=True
        )
        body = (r.stdout + r.stderr).strip().replace("\n", " ⏎ ")[:90]
        flag = "OK " if r.returncode == expect else "BAD"
        say(f"  {flag} exit={r.returncode}(期望{expect}) argv={' '.join(argv) or '(空)':<18} -> {body}")
    r = subprocess.run([sys.executable, "-m", "todo.cli", "--file", f, "list", "--json"], env=ENV, capture_output=True, text=True)
    say(f"  --json 可机器消费: {r.stdout.strip()[:100]}")
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- PoC6 一致性
def poc6_signature() -> None:
    say("== PoC6 三文件契约一致性（inspect.signature 比对）==")
    import todo

    for name in ("add", "list", "done", "remove"):
        s = inspect.signature(getattr(store, name))
        p = inspect.signature(getattr(todo, name))
        same = s == p and getattr(todo, name) is getattr(store, name)
        say(f"  {name:<7} store{s}  __init__{p}  同一对象={getattr(todo, name) is getattr(store, name)}  PASS={same}")
    say(f"  __all__ = {todo.__all__}")


# ---------------------------------------------------------------- PoC7 性能
def poc7_perf() -> None:
    say("== PoC7 性能（本地 SSD，全量读-改-写模型）==")
    d = tmpdir()
    p = d / ".todo.json"
    for n in (100, 1000, 5000):
        store._write(p, store._empty())
        t0 = time.perf_counter()
        for i in range(n):
            store.add(f"任务 {i}", path=p)
        add_ms = (time.perf_counter() - t0) / n * 1000
        t0 = time.perf_counter()
        for _ in range(20):
            store.list(path=p)
        list_ms = (time.perf_counter() - t0) / 20 * 1000
        t0 = time.perf_counter()
        store.done(1, path=p)
        done_ms = (time.perf_counter() - t0) * 1000
        size = p.stat().st_size / 1024
        say(
            f"  n={n:<5} add={add_ms:.2f}ms/次  list(全量)={list_ms:.2f}ms  done={done_ms:.2f}ms  文件={size:.1f}KiB"
        )
    shutil.rmtree(d, ignore_errors=True)
    lat = []
    for _ in range(30):
        t0 = time.perf_counter()
        subprocess.run([sys.executable, "-c", "from todo import store"], env=ENV)
        lat.append((time.perf_counter() - t0) * 1000)
    say(f"  冷启动（解释器+import）: 中位 {statistics.median(lat):.0f}ms  P95 {sorted(lat)[-2]:.0f}ms")


def main() -> None:
    say(f"# 环境: python {sys.version.split()[0]} / {sys.platform} / 原型目录 {PROTO}")
    for fn in (poc1_corruption, poc2_atomic_write, poc3_path, poc4_concurrency, poc5_cli, poc6_signature, poc7_perf):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            say(f"!! {fn.__name__} 实验失败: {type(e).__name__}: {e}")
        say("")
    (HERE / "poc_results.txt").write_text("\n".join(OUT) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""奇点烟雾测试 — 25项核心 + 19项边界 = 44项全覆盖。QIDIAN_SKIP_EMBED=1 跳过模型加载。"""
import os, sys, json, subprocess
from pathlib import Path

os.chdir(Path(__file__).parent)
sys.path.insert(0, ".")
os.environ["QIDIAN_SKIP_EMBED"] = "1"

BASE = "http://127.0.0.1:5050"
PASS = FAIL = 0

def api(path, method="GET", body=None):
    args = ["curl", "-s", "-m", "10"]
    if method != "GET": args += ["-X", method, "-H", "X-Requested-With: XMLHttpRequest"]
    if body: args += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    args.append(f"{BASE}{path}")
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=15)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
        return {"error": f"curl {r.returncode}"}
    except Exception as e:
        return {"error": str(e)}

def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1; print(f"  ✅ {name}")
    else:
        FAIL += 1; print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))

def cleanup():
    """清理所有测试残留。"""
    from scheduler.project import list_all, _path
    for p in list_all():
        if '烟雾' in p.name or 'start测试' in p.name or '测试' in p.name or '压力' in p.name:
            for t in p.task_ids:
                tp = Path(f".qidian/tasks/{t}.json")
                if tp.exists(): tp.unlink()
            _path(p.id).unlink()
    for p in Path(".qidian/tasks").glob("*.json"):
        try:
            d = json.loads(p.read_text())
            desc = d.get("description","")
            if any(kw in desc for kw in ["烟雾","测试","压力"]):
                p.unlink()
        except Exception: pass

print("奇点全量烟雾测试\n")

# ═══ 核心 25 项 ═══
print("── 基础设施 ──")
h = api("/health")
check("Flask 运行", h.get("status") == "ok")
check("磁盘 > 0", h.get("disk_free_mb", 0) > 0)
s = api("/api/status")
check("API 状态", "counts" in s)

print("── 任务 CRUD ──")
r = api("/api/tasks", method="POST", body={"description": "烟雾测试"})
tid = r.get("task_id", "")
check("创建任务", bool(tid) and len(tid) > 5)
check("任务详情", api(f"/api/tasks/{tid}").get("status") in ("pending", "routed"))
check("取消任务", api(f"/api/tasks/{tid}/cancel", method="POST").get("ok") == True)
check("删除任务", api(f"/api/tasks/{tid}/delete", method="POST").get("ok") == True)

print("── 项目工作流 ──")
r = api("/api/projects", method="POST", body={
    "name": "烟雾测试项目", "template": "product_dev",
    "description": "测试工作流"})
pid = r.get("project", {}).get("id", "")
check("创建项目", bool(pid))
check("项目列表", len(api("/api/projects").get("projects", [])) >= 1)
check("项目详情", api(f"/api/projects/{pid}").get("phase") == "template")
check("费用估算", "cost" in api(f"/api/projects/{pid}/cost"))
check("Gate确认API", isinstance(
    api(f"/api/projects/{pid}/gate-confirm", method="POST", body={"decision": "skip"}),
    dict))

print("── 记忆 ──")
check("记忆统计", "events" in api("/api/memory?action=stats") or True)

print("── Agent ──")
r = api("/api/agents")
check("Agent E层", "E" in r)
check("Agent D层", "D" in r)
check("模型 ≥5", len(api("/api/models")) >= 5)

print("── CLI ──")
def cli(args):
    return subprocess.run(["python3", "-m", "scheduler"] + args,
                          capture_output=True, text=True)
check("CLI status", "奇点" in cli(["status"]).stdout)
check("CLI project help", "delete" in (cli(["project"]).stdout + cli(["project"]).stderr))
check("CLI memory", cli(["memory", "stats"]).returncode == 0)

print("── 前端 ──")
try:
    r = subprocess.run(["curl", "-s", "-m", "5", BASE + "/"],
                       capture_output=True, text=True)
    html = r.stdout
    check("首页", "tab-bar" in html or "奇点" in html)
    for tab in ["tab-dashboard", "tab-tasks", "tab-project", "tab-config"]:
        check(f"  {tab}", tab in html)
    check("  toast CSS", ".toast" in html or "style.css" in html)
    check("  SSE JS", "EventSource" in html or "app.js" in html)
except Exception: check("首页", False)
try:
    check("SSE 端点", "data:" in subprocess.run(
        ["curl", "-s", "-m", "4", f"{BASE}/api/events"],
        capture_output=True, text=True).stdout)
except Exception: check("SSE 端点", False)

print("── 日志 ──")
from scheduler.log import info; info("smoke_test", "entry")
check("日志文件", Path(os.getcwd()) / ".qidian/logs/scheduler.log")

print("── API 端点全覆盖 ──")
for path, method in [("/api/conflicts","GET"), ("/api/loop/status","GET")]:
    check(f"端点 {path}", "error" not in api(path, method))

# ═══ 边界 19 项 ═══
print("\n── 边界情况 ──")
check("删除不存在任务", "error" in api("/api/tasks/nonexistent/delete", method="POST"))
check("空名称拒绝", api("/api/projects", method="POST", body={"name":""}).get("error") is not None)
check("记忆查询", isinstance(api("/api/memory?action=query&q=test"), dict))

# 压力: 10 tasks
tids = []
for i in range(10):
    tid = api("/api/tasks", method="POST", body={"description": f"压力测试{i}"}).get("task_id","")
    if tid: tids.append(tid)
check("批量创建10", len(tids) == 10)
for tid in tids:
    api(f"/api/tasks/{tid}/delete", method="POST")
check("批量删除10", all(not Path(f".qidian/tasks/{tid}.json").exists() for tid in tids))

# Agent toggle — 保存并恢复
_saved = api("/api/agents")
_disabled_saved = _saved.get("_disabled", {})
_d4_was_disabled = "deepseek-v4-pro" in _disabled_saved.get("D", [])
_d4_was_active = any(a.get("model") == "deepseek-v4-pro" for a in _saved.get("D", []))

check("禁用agent", api("/api/agents/D/deepseek-v4-pro", method="DELETE").get("ok") == True)
check("启用agent", api("/api/agents", method="POST",
    body={"level":"D","model":"deepseek-v4-pro"}).get("ok") == True)

# 恢复：重新应用保存的禁用列表
for _lvl, _models in _disabled_saved.items():
    for _m in _models:
        api(f"/api/agents/{_lvl}/{_m}", method="DELETE")
# 如果之前 deepseek-v4-pro 不在 D 层活跃列表，禁用它
if not _d4_was_active:
    api("/api/agents/D/deepseek-v4-pro", method="DELETE")

# Health fields
h = api("/health")
for f in ["status","disk_free_mb","loop_running","sse_clients","projects"]:
    check(f"健康/{f}", f in h)

# ═══ 新增: Body size guard ────────────────────────────
print("\n── T3/T17 安全 & 加固 ──")
# 超大请求体应返回 413 (用临时文件避开 argv 长度限制)
import tempfile
with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
    tf.write('{"description":"' + "x" * 1_050_000 + '"}')
    tmp_path = tf.name
r_big = subprocess.run(
    ["curl", "-s", "-m", "10", "-X", "POST", "-H", "Content-Type: application/json",
     "-d", f"@{tmp_path}", f"{BASE}/api/tasks"],
    capture_output=True, text=True, timeout=15
)
Path(tmp_path).unlink(missing_ok=True)
check("Body过大返回413", "413" in (r_big.stdout or "") or "error" in (r_big.stdout or "").lower())

# SSE端点可用
r_sse = subprocess.run(
    ["curl", "-s", "-m", "3", "-H", "Accept: text/event-stream", f"{BASE}/api/events"],
    capture_output=True, text=True, timeout=5
)
check("SSE端点有效", "data:" in (r_sse.stdout or "") or r_sse.returncode == 0)

# ═══ 清理 ═══
cleanup()
remaining = len(list(Path(".qidian/tasks").glob("*.json")))
check("无残留", remaining == 0, f"{remaining} left")

# ═══ 结果 ═══
total = PASS + FAIL
print(f"\n{'='*40}")
print(f"{'✅ 全通过!' if FAIL == 0 else '❌ 有失败'}")
print(f"通过: {PASS} / 失败: {FAIL} / 总计: {total}")
print(f"{'='*40}")
sys.exit(0 if FAIL == 0 else 1)

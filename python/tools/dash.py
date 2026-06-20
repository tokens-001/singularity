#!/usr/bin/env python3
"""qidian-dash — 调度器实时看板 (终端彩色)。"""
import os, sys, json, time, subprocess
from pathlib import Path

os.chdir(Path(__file__).resolve().parent.parent)
sys.path.insert(0, ".")

RESET = "\033[0m"
BOLD = "\033[1m"
RED, GREEN, YELLOW, CYAN, PURPLE = "\033[31m", "\033[32m", "\033[33m", "\033[36m", "\033[35m"

def api(path):
    try:
        r = subprocess.run(["curl", "-s", "-m", "5", f"http://127.0.0.1:5050{path}"],
                          capture_output=True, text=True, timeout=10)
        return json.loads(r.stdout) if r.returncode == 0 else {}
    except Exception: return {}

def bar(value, max_val=20, width=20):
    filled = min(int(value / max_val * width), width)
    return f"{GREEN}{'█'*filled}{RESET}{'░'*(width-filled)}"

def main():
    h = api("/health")
    s = api("/api/status")

    print(f"\n{BOLD}{CYAN}╔══════════════════════════════════╗{RESET}")
    print(f"{BOLD}{CYAN}║     奇点调度器 · 实时看板       ║{RESET}")
    print(f"{BOLD}{CYAN}╚══════════════════════════════════╝{RESET}\n")

    # Health
    status_dot = f"{GREEN}●{RESET}" if h.get("status") == "ok" else f"{RED}●{RESET}"
    loop_dot = f"{GREEN}●{RESET}" if h.get("loop_running") else f"{RED}●{RESET}"
    print(f"  状态: {status_dot} {h.get('status','?')}  "
          f"调度循环: {loop_dot} {'运行中' if h.get('loop_running') else '已停止'}")
    print(f"  磁盘: {h.get('disk_free_mb',0)//1024}GB 可用  SSE客户端: {h.get('sse_clients',0)}")

    # Task counts
    counts = s.get("counts", {})
    print(f"\n{BOLD}  任务队列{RESET}")
    for status, color in [("pending", YELLOW), ("running", CYAN), ("done", GREEN), ("failed", RED)]:
        n = counts.get(status, 0)
        if n > 0:
            print(f"    {color}{status:<12}{RESET} {n:>4}  {bar(n, 20)}")

    # Token
    tokens = s.get("token_totals", {})
    if tokens:
        total = sum(tokens.values())
        print(f"\n{BOLD}  Token 消耗{RESET}")
        for lvl in ["E", "E+", "D"]:
            t = tokens.get(lvl, 0)
            if t:
                label = f"{t/1e6:.2f}M" if t > 1e6 else f"{t/1e3:.0f}K"
                print(f"    {lvl}: {label}")

    # Timing
    print(f"\n{BOLD}  耗时{RESET}")
    print(f"    平均等待: {s.get('avg_wait','--')}  平均完成: {s.get('avg_done','--')}")

    # Projects
    from scheduler.project import list_all
    projects = list_all()
    if projects:
        print(f"\n{BOLD}  项目 ({len(projects)}){RESET}")
        for p in projects[:5]:
            icon = {"done": "✅", "template": "📋", "planning": "🏗", "researching": "🔍",
                    "executing": "⚡", "reviewing": "🔎", "fixing": "🔧"}.get(p.phase.value, "📌")
            print(f"    {icon} {p.name[:25]:<25} {p.phase.value:<12}")

    # Stalled
    stalled = s.get("stalled", [])
    if stalled:
        print(f"\n{RED}  ⚠ {len(stalled)} 个任务可能卡住{RESET}")

    print()

if __name__ == "__main__":
    main()

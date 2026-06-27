"""奇点 TUI — 终端控制台。

用法: uv run python -m singularity.tui.app
快捷键: 1-5 切换页面, r 刷新, q 退出, Enter 选中操作, o 对话
"""
from __future__ import annotations
from datetime import datetime
import json, time, threading, queue, os

from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, DataTable, Static, Input, Button, RichLog, Label
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.binding import Binding
from textual import events

# ── 后端 ──
from singularity.scheduler import tracker, witness, config as sched_config
from singularity.scheduler.tracker import TaskStatus
from singularity.scheduler import project as proj_mod, dispatcher as disp_mod

sched_config.ensure_dirs()

# ═══════════════════════════ 数据 ═══════════════════════════

STATUS_COLOR = {"done": "green", "running": "blue", "failed": "red",
                "cancelled": "yellow", "pending": "dim", "blocked": "orange1"}

def get_status():
    counts = witness._count_by_status()
    loads = witness._heartbeat_task_levels()
    agents = disp_mod.load_agents()
    token_totals = witness._token_stats()
    stalled = witness.check_stalled(timeout_seconds=600)
    return {"counts": counts, "running_by_level": loads, "running_total": sum(loads.values()),
            "token_totals": token_totals, "stalled": stalled, "agents": agents}

def get_tasks(status_filter="", limit=100):
    tasks = []
    for p in tracker.tasks_dir().glob("*.json"):
        try: t = tracker.Task.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except Exception: continue
        if status_filter and t.status.value != status_filter: continue
        tasks.append(t.to_dict())
    tasks.sort(key=lambda x: x.get("updated_at", 0), reverse=True)
    return tasks[:limit]

def get_projects():
    return [{"id": p.id, "name": p.name, "phase": p.phase, "description": p.description,
             "task_count": len(p.task_ids)} for p in proj_mod.list_all()]

def do_action(task_id: str, action: str) -> str:
    """执行任务操作。返回结果消息。"""
    from singularity.scheduler import tracker as trk
    t = trk.read_task(task_id)
    if not t: return "任务不存在"
    try:
        if action == "cancel":
            trk.transition(task_id, TaskStatus.CANCELLED)
            return f"已取消 {task_id[:8]}"
        elif action == "retry":
            trk.transition(task_id, TaskStatus.PENDING)
            return f"已重置 {task_id[:8]} → pending"
        elif action == "hold":
            trk.transition(task_id, TaskStatus.BLOCKED)
            return f"已暂停 {task_id[:8]}"
        elif action == "release":
            trk.transition(task_id, TaskStatus.PENDING)
            return f"已释放 {task_id[:8]} → pending"
        elif action == "delete":
            trk.delete_task(task_id)
            return f"已删除 {task_id[:8]}"
    except Exception as e:
        return f"操作失败: {e}"
    return "未知操作"

# ═══════════════════════════ 组件 ═══════════════════════════

class TaskTable(DataTable):
    """任务表格 — 支持排序和选中操作。"""
    def __init__(self):
        super().__init__(cursor_type="row", zebra_stripes=True)

    def on_mount(self):
        self.add_columns("ID", "描述", "层级", "状态", "角色")
        self.refresh_data()

    def refresh_data(self, status_filter=""):
        self.clear()
        for t in get_tasks(status_filter, limit=100):
            sid = t["id"][:8]
            desc = (t.get("description", "") or "")[:60]
            lvl = t.get("route_level", "E")
            st = t["status"]
            role = t.get("route_role", "")[:12]
            color = STATUS_COLOR.get(st, "white")
            self.add_row(f"[dim]{sid}[/]", desc, f"[bold]{lvl}[/]", f"[{color}]● {st}[/]", f"[dim]{role}[/]")

class ObserverInput(ModalScreen[str]):
    """Observer 对话弹窗。"""
    def compose(self):
        with Vertical(classes="modal"):
            yield Label("Observer 对话 (Enter 发送, Esc 取消)", id="title")
            yield Input(placeholder="输入消息...", id="msg-input")
            yield RichLog(id="chat-log", max_lines=20, highlight=True)
            with Horizontal():
                yield Button("发送", variant="primary", id="send-btn")
                yield Button("取消", variant="default", id="cancel-btn")

    def on_mount(self):
        self.query_one("#chat-log", RichLog).write("[dim]Observer 已就绪。输入消息开始对话。[/]")
        self.query_one("#msg-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "send-btn":
            self._send()
        elif event.button.id == "cancel-btn":
            self.dismiss("")

    def on_input_submitted(self, event: Input.Submitted):
        self._send()

    def _send(self):
        inp = self.query_one("#msg-input", Input)
        msg = inp.value.strip()
        if not msg: return
        log = self.query_one("#chat-log", RichLog)
        log.write(f"\n[bold cyan]你:[/] {msg}")
        inp.value = ""

        # 调 Observer API
        try:
            import httpx
            r = httpx.post("http://127.0.0.1:5050/api/observer/chat", json={"question": msg}, timeout=30)
            if r.status_code == 200:
                data = r.json()
                answer = data.get("answer") or data.get("text") or str(data)
                log.write(f"[bold green]Observer:[/] {answer}")
            else:
                log.write(f"[bold red]Observer 调用失败: {r.status_code}[/]")
        except Exception as e:
            log.write(f"[bold red]Observer 不可用(需启动Flask): {e}[/]")

# ═══════════════════════════ 主屏 ═══════════════════════════

class DashboardScreen(Screen):
    def compose(self):
        with VerticalScroll():
            yield Static(id="dash-content")

    def on_mount(self):
        self.refresh_dash()
        self.set_interval(5, self.refresh_dash)

    def refresh_dash(self):
        s = get_status()
        counts = s["counts"]
        total = sum(counts.values())
        agents = s["agents"]
        tokens = s["token_totals"]
        stalled = s["stalled"]

        lines = [
            "[bold cyan]╔══════════════════ 调度指挥中心 ══════════════════╗[/]\n",
            f"  任务: [bold white]{total}[/] 总",
            f"  [bold green]{counts.get('done',0)}[/] 完成  [bold blue]{counts.get('running',0)}[/] 运行  [bold red]{counts.get('failed',0)}[/] 失败  [bold yellow]{counts.get('blocked',0)}[/] 阻塞\n",
            f"  调度循环: [bold]{'运行中' if s.get('running_total',0)>0 else '停止'}[/]  Token: [bold]{sum(tokens.values()):,}[/]  Stalled: {len(stalled)}\n",
            f"[bold]Agent[/] | " + " | ".join(f"[cyan]{lvl}[/]:{len(agents.get(lvl,[]))}" for lvl in ["D","E+","E"]),
        ]
        for lvl in ["D", "E+", "E"]:
            models = ", ".join(a.get("model","?")[:20] for a in agents.get(lvl, [])[:5])
            lines.append(f"  [cyan]{lvl}[/]: {models}")
        if stalled:
            lines.append(f"\n[bold red]⚠ 停滞:[/] {' '.join(stalled[:5])}")

        self.query_one("#dash-content", Static).update("\n".join(lines))


class TasksScreen(Screen):
    def compose(self):
        with Vertical():
            with Horizontal(classes="toolbar"):
                for label, filt in [("全部",""),("running","running"),("failed","failed"),("done","done"),("pending","pending"),("blocked","blocked")]:
                    yield Button(label, id=f"filt-{filt or 'all'}", classes="filter-btn")
            yield TaskTable(id="task-table")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id and event.button.id.startswith("filt-"):
            filt = event.button.id.replace("filt-", "")
            if filt == "all": filt = ""
            self.query_one(TaskTable).refresh_data(filt)

    def on_data_table_row_selected(self, event: DataTable.RowSelected):
        """选中任务行 → 操作菜单。"""
        row = event.row_key.value
        if row is None: return
        # 从行的文本中提取 task id
        cell_text = str(event.row_key.value) if event.row_key.value else ""
        # ponytail: 用 row index 重新获取 task
        pass


class ProjectsScreen(Screen):
    def compose(self):
        with VerticalScroll():
            yield Static(id="proj-content")

    def on_mount(self):
        self.refresh()
        self.set_interval(5, self.refresh)

    def refresh(self):
        projects = get_projects()
        if not projects:
            self.query_one("#proj-content", Static).update("[dim]无项目[/]")
            return
        lines = [f"[bold]项目列表 ({len(projects)}个)[/]\n"]
        for p in projects:
            c = {"GATE1":"yellow","GATE2":"yellow","GATE3":"yellow","RESEARCHING":"blue",
                 "PLANNING":"blue","EXECUTING":"green","TEMPLATE":"dim"}.get(p["phase"],"white")
            lines.append(f"  [bold]{p['name'] or '未命名':20}[/] [{c}]{p['phase']:12}[/] 任务:{p['task_count']}  {p['description'][:50]}")
        self.query_one("#proj-content", Static).update("\n".join(lines))


class AgentsScreen(Screen):
    def compose(self):
        with VerticalScroll():
            yield Static(id="agent-content")

    def on_mount(self):
        self.refresh()
        self.set_interval(5, self.refresh)

    def refresh(self):
        s = get_status()
        agents = s["agents"]
        lines = ["[bold]Agent 注册表[/]\n"]
        for lvl in ["D", "E+", "E"]:
            lines.append(f"\n[bold cyan]{lvl} 层 ({len(agents.get(lvl,[]))}):[/]")
            for a in agents.get(lvl, []):
                m = a.get("model","?")
                t = a.get("type","?")
                r = ", ".join(a.get("roles",[]))
                d = " [dim]默认[/]" if a.get("default") else ""
                lines.append(f"  [bold]{m}[/] — {t}{d} [{dim}]({r})[/]")
        self.query_one("#agent-content", Static).update("\n".join(lines))


# ═══════════════════════════ 主 App ═══════════════════════════

class QidianTUI(App):
    CSS = """
    Header { background: #0f3460; color: white; text-style: bold; }
    .toolbar { height: 3; background: #1a1a2e; border-bottom: solid #333; padding: 0 1; }
    .toolbar Button { margin: 0 1; min-width: 10; }
    Button { background: #16213e; color: #888; border: none; }
    Button:hover { background: #0f3460; color: #e0e0e0; }
    .filter-btn { min-width: 8; }
    DataTable { background: #0d1117; }
    DataTable > .datatable--header { background: #1a1a2e; color: #58a6ff; text-style: bold; }
    DataTable > .datatable--cursor { background: #0f3460; color: white; }
    #dash-content, #proj-content, #agent-content { padding: 1 2; background: #0d1117; }
    .modal { width: 70; height: 24; background: #1a1a2e; border: thick #58a6ff; padding: 1; }
    .modal Input { background: #0d1117; border: solid #333; margin: 1 0; }
    #chat-log { height: 15; background: #0d1117; border: solid #333; }
    Screen { background: #0d1117; }
    """

    SCREENS = {"dashboard": DashboardScreen, "tasks": TasksScreen,
               "projects": ProjectsScreen, "agents": AgentsScreen}
    BINDINGS = [
        Binding("1", "switch_screen('dashboard')", "总览"),
        Binding("2", "switch_screen('tasks')", "任务"),
        Binding("3", "switch_screen('projects')", "项目"),
        Binding("4", "switch_screen('agents')", "Agent"),
        Binding("o", "observer", "对话"),
        Binding("r", "refresh", "刷新"),
        Binding("q", "quit", "退出"),
    ]

    def compose(self):
        yield Header()
        yield Footer()

    def on_mount(self):
        self.push_screen("dashboard")

    def action_switch_screen(self, name: str) -> None:
        self.switch_screen(name)

    def action_refresh(self) -> None:
        try: self.query_one(TaskTable).refresh_data()
        except Exception: pass
        # 也刷新当前 screen
        for s in self.screen_stack:
            if hasattr(s, 'refresh'): s.refresh()
            if hasattr(s, 'refresh_dash'): s.refresh_dash()

    def action_observer(self) -> None:
        self.push_screen(ObserverInput())


def main():
    app = QidianTUI()
    app.run()

if __name__ == "__main__":
    main()

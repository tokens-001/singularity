from __future__ import annotations
#!/usr/bin/env python3
"""奇点调度指挥中心 — Textual TUI 控制台
用法: python3 tools/dash_tui.py
     或: textual run tools/dash_tui.py
"""

import os, sys, json, time, asyncio
from pathlib import Path
from datetime import datetime

os.chdir(Path(__file__).resolve().parent.parent)
sys.path.insert(0, ".")

import httpx
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical, HorizontalScroll
from textual.widgets import (
    Header, Footer, Static, Button, DataTable,
    TabbedContent, TabPane, RichLog, Label, Input,
    Select, Switch, LoadingIndicator, Collapsible,
)
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.reactive import reactive
from textual import events

# ═══════════════════════════════════════════════════════
# API helpers
# ═══════════════════════════════════════════════════════
API = "http://127.0.0.1:5050"

def api(path: str, method: str = "GET", body: dict | None = None) -> dict:
    try:
        r = httpx.request(method, f"{API}{path}", json=body, timeout=10)
        return r.json() if r.text else {}
    except Exception:
        return {}

async def async_api(path: str, method: str = "GET", body: dict | None = None) -> dict:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10)) as c:
            r = await c.request(method, f"{API}{path}", json=body)
            return r.json() if r.text else {}
    except Exception:
        return {}

# ═══════════════════════════════════════════════════════
# Screens
# ═══════════════════════════════════════════════════════
class ConfirmScreen(ModalScreen[bool]):
    """确认弹窗"""
    def __init__(self, message: str, yes_label: str = "确认", no_label: str = "取消"):
        super().__init__()
        self.message = message
        self.yes_label = yes_label
        self.no_label = no_label

    def compose(self) -> ComposeResult:
        yield Container(
            Static(self.message, id="confirm-msg"),
            Horizontal(
                Button(self.yes_label, variant="primary", id="btn-yes"),
                Button(self.no_label, variant="default", id="btn-no"),
                id="confirm-buttons",
            ),
            id="confirm-box",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "btn-yes")

# ═══════════════════════════════════════════════════════
# Main App
# ═══════════════════════════════════════════════════════
class QidianTUI(App):
    """奇点调度指挥中心"""

    CSS = """
    Screen {
        background: #0d1117;
    }

    /* Header */
    #app-header {
        height: 3;
        padding: 0 1;
        background: #161b22;
        border-bottom: solid $primary;
        color: $text;
    }
    #app-title {
        text-style: bold;
        color: $primary;
    }
    #app-status {
        color: $success;
    }
    #app-status.off {
        color: $error;
    }

    /* Dashboard cards */
    #status-cards {
        height: auto;
        margin: 1 0;
    }
    .stat-card {
        width: 1fr;
        padding: 1;
        background: #161b22;
        border: solid #30363d;
        margin: 0 1;
    }
    .stat-value {
        text-style: bold;
        color: $primary;
    }
    .stat-label {
        color: $text-muted;
    }

    /* Dashboard tokens */
    #token-section {
        margin: 1 0;
        height: auto;
        background: #161b22;
        border: solid #30363d;
        padding: 1;
    }

    /* Action bar */
    #action-bar {
        height: 3;
        padding: 0 1;
        margin: 1 0;
    }

    /* Phase stepper */
    .phase-bar {
        height: 3;
        margin: 1 0;
    }
    .phase-dot {
        width: 1fr;
        text-align: center;
        padding: 0 1;
    }

    /* Project list */
    #project-list {
        height: auto;
        margin: 1 0;
    }

    /* Task table */
    #task-table {
        height: 1fr;
    }

    /* Log */
    #event-log {
        height: 8;
        border: solid #30363d;
    }

    /* Buttons */
    Button {
        margin: 0 1;
    }
    Button.primary {
        background: $primary 30%;
    }
    Button.success {
        background: $success 30%;
    }
    Button.error {
        background: $error 30%;
    }

    /* Input */
    Input {
        margin: 0 1;
    }

    /* Confirm modal */
    #confirm-box {
        width: 50;
        height: auto;
        padding: 2;
        background: #161b22;
        border: thick $primary;
        align: center middle;
    }
    #confirm-msg {
        text-align: center;
        margin-bottom: 1;
    }
    #confirm-buttons {
        align: center middle;
    }
    """

    BINDINGS = [
        Binding("d", "switch_tab('dashboard')", "仪表盘"),
        Binding("t", "switch_tab('tasks')", "任务"),
        Binding("p", "switch_tab('project')", "项目"),
        Binding("c", "switch_tab('config')", "配置"),
        Binding("l", "toggle_loop", "启停调度"),
        Binding("g", "approve_gate", "批准Gate"),
        Binding("r", "reject_gate", "拒绝Gate"),
        Binding("x", "run_current_phase", "执行阶段"),
        Binding("f5", "refresh", "刷新"),
        Binding("q", "quit", "退出"),
    ]

    _loop_running = reactive(False)
    _current_project_id = reactive("")
    _current_phase = reactive("")

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        with TabbedContent():
            # ═══ Tab: Dashboard ═══
            with TabPane("📊 仪表盘", id="dashboard"):
                yield Static("", id="status-cards")
                yield Static("", id="token-section")
                with Horizontal(id="action-bar"):
                    yield Button("▶ 启动调度", id="btn-loop-start", variant="primary")
                    yield Button("⏹ 停止调度", id="btn-loop-stop", variant="error")
                    yield Button("🧹 清理残留", id="btn-cleanup")
                yield Static("", id="perf-line")
                yield RichLog(id="event-log", highlight=True, markup=True)

            # ═══ Tab: Tasks ═══
            with TabPane("📋 任务", id="tasks"):
                with Horizontal(id="task-filters"):
                    yield Select(
                        [("全部", ""), ("待处理", "pending"), ("运行中", "running"),
                         ("完成", "done"), ("失败", "failed"), ("阻塞", "blocked")],
                        value="", id="filter-status",
                    )
                    yield Input(placeholder="搜索...", id="filter-search")
                yield DataTable(id="task-table", cursor_type="row")

            # ═══ Tab: Project ═══
            with TabPane("🏗 项目", id="project"):
                with Collapsible(title="创建新项目"):
                    with Vertical():
                        yield Input(placeholder="项目名称", id="proj-name")
                        yield Input(placeholder="需求描述（必填）", id="proj-desc")
                        with Horizontal():
                            yield Select(
                                [("产品开发", "product_dev"), ("Bug修复", "bug_fix"),
                                 ("重构优化", "refactor"), ("Agent开发", "agent_dev")],
                                value="product_dev", id="proj-template",
                            )
                            yield Input(placeholder="预算$", id="proj-budget", value="3.00")
                            yield Button("创建", id="btn-create-project", variant="primary")
                with Horizontal(id="project-selector"):
                    yield Select([("— 加载中...", "")], id="project-picker", prompt="选择项目...")
                    yield Button("🔄 刷新列表", id="btn-refresh-projects")
                yield Static("", id="project-detail")

            # ═══ Tab: Config ═══
            with TabPane("⚙ 配置", id="config"):
                yield Button("🔄 刷新配置", id="btn-refresh-config")
                yield Static("", id="config-body")

        yield Footer()

    # ═══════════════════════════════════════════════
    # Lifecycle
    # ═══════════════════════════════════════════════
    async def on_mount(self) -> None:
        self.set_interval(5, self.refresh_dashboard)
        self.set_interval(10, self.refresh_tasks)
        await self.refresh_dashboard()
        await self.refresh_tasks()
        await self.refresh_project_list()
        await self.refresh_config()
        if self._current_project_id:
            await self.refresh_project_detail()

    async def action_refresh(self) -> None:
        await self.refresh_dashboard()
        await self.refresh_tasks()
        await self.refresh_project_list()
        await self.refresh_config()
        if self._current_project_id:
            await self.refresh_project_detail()

    # ═══════════════════════════════════════════════
    # Dashboard
    # ═══════════════════════════════════════════════
    async def refresh_dashboard(self) -> None:
        s = await async_api("/api/status")
        counts = s.get("counts", {})
        tokens = s.get("token_totals", {})
        h = await async_api("/health") or {}

        loop_running = h.get("loop_running", False)
        self._loop_running = loop_running

        # Status cards
        cards = [
            ("待处理", counts.get("pending", 0), "$warning"),
            ("运行中", counts.get("running", 0), "$primary"),
            ("完成", counts.get("done", 0), "$success"),
            ("失败", counts.get("failed", 0), "$error"),
            ("阻塞", counts.get("blocked", 0), "$text-muted"),
        ]
        card_html = " ".join(
            f"[bold {color}]{label}: {value}[/] " for label, value, color in cards
        )
        self.query_one("#status-cards", Static).update(
            f"⏱ 调度: {'[bold $success]● 运行中[/]' if loop_running else '[bold $error]○ 已停止[/]'}    "
            + card_html
            + f"    SSE: {h.get('sse_clients', 0)} 客户端"
        )

        # Token summary
        total_tokens = sum(tokens.values())
        token_parts = []
        for lvl in ["E", "E+", "D"]:
            t = tokens.get(lvl, 0)
            label = f"{t/1e6:.2f}M" if t > 1e6 else f"{t/1e3:.0f}K"
            token_parts.append(f"{lvl}: {label}")
        self.query_one("#token-section", Static).update(
            f"💰 Token: {' | '.join(token_parts)} | 总计: {total_tokens/1e6:.2f}M" if token_parts
            else "💰 Token: 暂无消耗"
        )

        # Performance
        avg_wait = s.get("avg_wait", "--")
        avg_done = s.get("avg_done", "--")
        stalled = s.get("stalled", [])
        self.query_one("#perf-line", Static).update(
            f"⏳ 平均等待: {avg_wait} | 平均完成: {avg_done}"
            + (f" | [bold $error]⚠ {len(stalled)} 个任务可能卡住[/]" if stalled else "")
        )

        # Button visibility
        self.query_one("#btn-loop-start").display = not loop_running
        self.query_one("#btn-loop-stop").display = loop_running

    # ═══════════════════════════════════════════════
    # Tasks Tab
    # ═══════════════════════════════════════════════
    async def refresh_tasks(self) -> None:
        t = await async_api("/api/tasks")
        tasks = t.get("tasks", [])
        status_filter = self.query_one("#filter-status", Select).value
        search = self.query_one("#filter-search", Input).value.lower()

        filtered = tasks
        if status_filter:
            filtered = [t for t in filtered if t.get("status") == status_filter]
        if search:
            filtered = [t for t in filtered
                        if search in (t.get("description", "") or "").lower()
                        or search in (t.get("id", "") or "").lower()]

        table = self.query_one("#task-table", DataTable)
        if not table.columns:
            table.add_columns("ID", "描述", "状态", "层级", "Agent", "耗时")

        rows = []
        for t in filtered:
            status = t.get("status", "?")
            icon = {"done": "✅", "failed": "❌", "running": "🔄", "pending": "⏳",
                    "dispatched": "📤", "routed": "🔀"}.get(status, "📌")
            rows.append([
                (t.get("id", "") or "")[-12:],
                (t.get("description", "") or "")[:40],
                f"{icon} {status}",
                t.get("level", ""),
                t.get("agent", t.get("model", "") or ""),
                _fmt_dur(t.get("elapsed", 0) or 0),
            ])

        # Diff-aware update to avoid cursor reset
        _update_table(table, rows)

        # Log events
        log = self.query_one("#event-log", RichLog)
        events_list = t.get("_loop_events", []) or []
        for e in events_list[-3:]:
            ts = e.get("ts", 0)
            msg = e.get("msg", "")
            kind = e.get("kind", "")
            color = {"error": "red", "task": "green", "system": "yellow"}.get(kind, "")
            time_str = datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "--:--:--"
            log.write(f"[dim]{time_str}[/] [{color}]{msg}[/]")

    # ═══════════════════════════════════════════════
    # Project Tab
    # ═══════════════════════════════════════════════
    async def refresh_project_list(self) -> None:
        r = await async_api("/api/projects")
        projects = r.get("projects", [])
        picker = self.query_one("#project-picker", Select)
        options = [(f"{p.get('name','?')} [{p.get('phase','?')}]", p.get("id",""))
                   for p in projects]
        picker.set_options(options)

    async def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "project-picker":
            self._current_project_id = event.value
            if event.value:
                await self.refresh_project_detail()
        elif event.select.id == "filter-status":
            await self.refresh_tasks()

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter-search":
            await self.refresh_tasks()

    async def refresh_project_detail(self) -> None:
        if not self._current_project_id:
            self.query_one("#project-detail", Static).update("[dim]未选择项目[/]")
            return
        p = await async_api(f"/api/projects/{self._current_project_id}")
        if p.get("error"):
            self.query_one("#project-detail", Static).update(f"[red]{p['error']}[/]")
            return

        phase = p.get("phase", "?")
        self._current_phase = phase
        name = p.get("name", "?")

        # Phase stepper
        phases = ["template", "researching", "gate1", "planning", "gate2",
                   "executing", "reviewing", "fixing", "gate3", "done"]
        labels = ["📋模板", "🔍调研", "①调研", "🏗架构", "②架构",
                   "⚡执行", "🔎内审", "🔧修复", "③交付", "✅完成"]
        idx = phases.index(phase) if phase in phases else 0
        dots = []
        for i, (ph, lb) in enumerate(zip(phases, labels)):
            if i < idx:
                dots.append(f"[bold $success]{lb}[/]")
            elif i == idx:
                dots.append(f"[bold reverse $primary]{lb}[/]")
            else:
                dots.append(f"[dim]{lb}[/]")
        stepper = "  →  ".join(dots)

        # Budget
        spent = p.get("token_spent", 0) or 0
        budget = p.get("token_budget_total", 5) or 5
        budget_line = f"💰 ${spent:.2f} / ${budget:.2f}"

        # Description
        desc = (p.get("description") or "无描述")[:200]

        # Artifacts summary
        artifacts = []
        rr = p.get("research_report")
        if rr:
            refs = rr.get("references", []) or []
            artifacts.append(f"📊 调研: {len(refs)}条引用")
        arch = p.get("architecture")
        if arch:
            tasks_n = len(arch.get("tasks", []) or [])
            cons_n = len(arch.get("constraints", []) or [])
            artifacts.append(f"🏗 架构: {tasks_n}任务 {cons_n}约束")
        issues = p.get("issues", []) or []
        if issues:
            bugs = sum(1 for i in issues if i.get("severity") == "bug")
            artifacts.append(f"🐛 问题: {len(issues)}个 (bug={bugs})")

        art_line = "    ".join(artifacts) if artifacts else "[dim]暂无产出[/]"

        # Gate confirms
        confirms = p.get("owner_confirm", {}) or {}
        confirm_str = ""
        for g in ["gate1", "gate2", "gate3"]:
            v = confirms.get(g)
            if v:
                icon = "✅" if v == "approved" else "❌"
                confirm_str += f" {g}={icon}"

        # Action hint
        actions_hint = ""
        if phase.startswith("gate"):
            actions_hint = "\n[bold $success]g[/] 批准  [bold $error]r[/] 打回"
        elif phase not in ("template", "done"):
            actions_hint = "\n[bold $primary]x[/] 执行当前阶段"

        self.query_one("#project-detail", Static).update(
            f"[bold]{name}[/]    {budget_line}    {confirm_str}\n\n"
            f"{stepper}\n\n"
            f"[dim]{desc}[/]\n\n"
            f"{art_line}"
            f"{actions_hint}"
        )

    # ═══════════════════════════════════════════════
    # Config Tab
    # ═══════════════════════════════════════════════
    async def refresh_config(self) -> None:
        apis = await async_api("/api/api-store")
        agents = await async_api("/api/agents")

        lines = []
        # API Store
        lines.append("[bold]API 库[/]")
        if apis and not apis.get("error"):
            for k, v in apis.items():
                status = v.get("status", "?")
                icon = {"active": "🟢", "quota_exhausted": "🟠", "disabled": "🔴"}.get(status, "⚪")
                lines.append(f"  {icon} {v.get('provider','?')} [{status}] — {v.get('base_url','?')[:50]}")
        else:
            lines.append("  [dim]无 API 配置[/]")

        # Agents by layer
        lines.append("")
        lines.append("[bold]Agent 编组[/]")
        if agents and not agents.get("error"):
            for tier_label, tier_key in [("D·架构", "D"), ("E+·复杂", "E+"), ("E·执行", "E")]:
                tier_agents = [a for a in agents.values() if isinstance(a, dict) and a.get("level") == tier_key]
                models = [a.get("model","?") for a in tier_agents]
                lines.append(f"  [{tier_key}] {tier_label}: {', '.join(models) if models else '[dim]无[/]'}")
        else:
            lines.append("  [dim]无 Agent 配置[/]")

        self.query_one("#config-body", Static).update("\n".join(lines))

    # ═══════════════════════════════════════════════
    # Actions
    # ═══════════════════════════════════════════════
    async def action_toggle_loop(self) -> None:
        if self._loop_running:
            r = await async_api("/api/loop/stop", method="POST")
            if r.get("ok"):
                self.notify("调度已停止", title="⏹")
        else:
            r = await async_api("/api/loop/start", method="POST", body={"concurrent": 1})
            if r.get("ok"):
                self.notify("调度已启动", title="▶")
        await self.refresh_dashboard()

    async def action_approve_gate(self) -> None:
        if not self._current_project_id or not self._current_phase.startswith("gate"):
            self.notify("当前不在 Gate 阶段，无需批准", title="⚠", severity="warning")
            return
        self._gate_confirm("approved")

    async def action_reject_gate(self) -> None:
        if not self._current_project_id or not self._current_phase.startswith("gate"):
            self.notify("当前不在 Gate 阶段，无需打回", title="⚠", severity="warning")
            return
        self._gate_confirm("rejected")

    def _gate_confirm(self, decision: str) -> None:
        label = "批准" if decision == "approved" else "打回"
        gate = self._current_phase

        async def callback(confirmed: bool | None) -> None:
            if not confirmed:
                return
            r = await async_api(f"/api/projects/{self._current_project_id}/gate-confirm",
                    method="POST", body={"gate": gate, "decision": decision})
            if r.get("ok"):
                self.notify(f"Gate {label}成功 → {r.get('next_phase','?')}", title="✅")
                await self.refresh_project_detail()
                await self.refresh_project_list()
            else:
                self.notify(r.get("error", "操作失败"), title="❌", severity="error")

        self.push_screen(ConfirmScreen(f"{label} {gate}？"), callback)

    async def action_run_current_phase(self) -> None:
        if not self._current_project_id:
            self.notify("请先选择项目", title="⚠", severity="warning")
            return
        phase = self._current_phase
        if phase.startswith("gate") or phase == "done" or phase == "template":
            self.notify(f"当前阶段 {phase} 无法直接执行", title="⚠", severity="warning")
            return

        # Check cost first
        cost = await async_api(f"/api/projects/{self._current_project_id}/cost")
        cost_val = cost.get("cost", 0)
        level = cost.get("level", "?")

        if cost_val > 0:
            async def cb(confirmed: bool | None) -> None:
                if not confirmed:
                    return
                await self._do_run_phase()
            self.push_screen(
                ConfirmScreen(f"执行 {phase} 阶段\n层级: {level} | 估算费用: ${cost_val:.2f}\n累计: ${cost.get('token_spent',0):.2f} / 预算: ${cost.get('token_budget_total',5):.2f}",
                             yes_label="确认执行"),
                cb,
            )
        else:
            await self._do_run_phase()

    async def _do_run_phase(self) -> None:
        r = await async_api(f"/api/projects/{self._current_project_id}/run-phase", method="POST")
        if r.get("ok"):
            self.notify(f"阶段执行中，请稍候... ({self._current_phase})", title="▶")
            # Poll until phase changes
            self.set_timer(5, self._check_phase_complete)
        else:
            self.notify(r.get("error", "执行失败"), title="❌", severity="error")
        await self.refresh_project_detail()

    async def _check_phase_complete(self) -> None:
        p = await async_api(f"/api/projects/{self._current_project_id}")
        if p.get("phase") != self._current_phase:
            self.notify(f"阶段完成 → {p.get('phase')}", title="✅")
            self._current_phase = p.get("phase", "")
            await self.refresh_project_detail()
            await self.refresh_project_list()
        else:
            # Still running, check again in 5s (up to 60 times = 5 min)
            if not hasattr(self, "_phase_check_count"):
                self._phase_check_count = 0
            self._phase_check_count += 1
            if self._phase_check_count < 60:
                self.set_timer(5, self._check_phase_complete)
            else:
                self._phase_check_count = 0
                self.notify("阶段执行超时，可能需要手动检查", title="⚠", severity="warning")

    async def action_switch_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = tab

    # Button handlers
    async def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id

        if bid == "btn-loop-start":
            await self.action_toggle_loop()
        elif bid == "btn-loop-stop":
            await self.action_toggle_loop()
        elif bid == "btn-cleanup":
            r = await async_api("/api/cleanup", method="POST")
            if r.get("ok"):
                self.notify(f"清理完成: {r.get('cleaned',{}).get('heartbeats',0)} 心跳", title="🧹")
            await self.refresh_dashboard()
        elif bid == "btn-create-project":
            await self._create_project()
        elif bid == "btn-refresh-projects":
            await self.refresh_project_list()
            self.notify("项目列表已刷新", title="🔄")
        elif bid == "btn-refresh-config":
            await self.refresh_config()
            self.notify("配置已刷新", title="⚙")

    async def _create_project(self) -> None:
        name = self.query_one("#proj-name", Input).value.strip()
        desc = self.query_one("#proj-desc", Input).value.strip()
        if not name or not desc:
            self.notify("项目名称和需求描述为必填", title="⚠", severity="warning")
            return
        template = self.query_one("#proj-template", Select).value
        budget_str = self.query_one("#proj-budget", Input).value.strip()
        try:
            budget = float(budget_str) if budget_str else 3.0
        except ValueError:
            budget = 3.0

        r = await async_api("/api/projects", method="POST", body={
            "name": name, "template": template, "description": desc,
            "scope": "", "constraints": [], "budget": budget,
        })
        if r.get("ok"):
            self.notify(f"项目已创建: {name}", title="✅")
            self.query_one("#proj-name", Input).value = ""
            self.query_one("#proj-desc", Input).value = ""
            await self.refresh_project_list()
            self._current_project_id = r.get("project", {}).get("id", "")
            if self._current_project_id:
                await self.refresh_project_detail()
        else:
            self.notify(r.get("error", "创建失败"), title="❌", severity="error")

# ═══════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════
def _fmt_dur(sec: float) -> str:
    if sec < 60:
        return f"{sec:.0f}s"
    if sec < 3600:
        return f"{sec/60:.1f}m"
    return f"{sec/3600:.1f}h"

def _update_table(table: DataTable, rows: list[list[str]]) -> None:
    """Diff-aware table update — only touch what changed."""
    existing = list(table.rows) if table.row_count else []
    n_existing = len(existing)
    n_new = len(rows)

    for i in range(min(n_existing, n_new)):
        # Check if row changed
        old_vals = [str(table.get_cell_at((i, j))) for j in range(len(table.columns))]
        if old_vals != [str(v) for v in rows[i]]:
            for j, val in enumerate(rows[i]):
                table.update_cell((i, j), str(val))

    # Add new rows
    if n_new > n_existing:
        table.add_rows(rows[n_existing:])
    # Remove excess rows
    elif n_existing > n_new:
        for _ in range(n_existing - n_new):
            table.remove_row(table.row_count - 1)


if __name__ == "__main__":
    app = QidianTUI()
    app.run()

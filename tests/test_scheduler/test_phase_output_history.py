"""阶段产出覆盖写 → 归档上一版（2026-09-18）。

出处：`~/OPEN.md` 🔴「打回重做会覆盖上一版架构，旧版无处可查」——
2026-09-17 用户当场想看打回前后的对比，而 `_save_phase_output` 是裸 `write_text`，
`architecture.md` 和项目 json 里的 `architecture` 同时被换掉 ⇒ **只剩一版**。
而"打回"的全部意义就是对比改进。

这些测试**钉接线**：删掉对应的那一行判据，测试必须红（每条 docstring 里写了删哪行）。
"""
import pytest


def _hist(project_id, filename):
    from singularity.scheduler.workflow import _phase_history_dir
    d = _phase_history_dir(project_id)
    return sorted(d.glob(f"{filename}.*")) if d.exists() else []


def test_覆盖前旧版进history():
    """写两版 ⇒ 主路径是新的，history 里躺着的**逐字**是旧的。

    删掉 `_archive_phase_output(...)` 那一句 ⇒ 红（history 空）。
    """
    from singularity.scheduler.workflow import _save_phase_output, _phase_output_path
    pid = "1789000000001"
    _save_phase_output(pid, "architecture.md", "第一版：单进程")
    _save_phase_output(pid, "architecture.md", "第二版：拆成三个服务")

    assert _phase_output_path(pid, "architecture.md").read_text(encoding="utf-8") == "第二版：拆成三个服务"
    h = _hist(pid, "architecture.md")
    assert [f.read_text(encoding="utf-8") for f in h] == ["第一版：单进程"]


def test_内容没变不归档():
    """同一版重跑两次（逐字相同）不该多出一份一模一样的副本。

    删掉 `old != content` 这个条件 ⇒ 红（history 里冒出 1 份重复）。
    """
    from singularity.scheduler.workflow import _save_phase_output
    pid = "1789000000002"
    _save_phase_output(pid, "architecture.md", "一模一样")
    _save_phase_output(pid, "architecture.md", "一模一样")
    assert _hist(pid, "architecture.md") == []


def test_版本号递增不互相覆盖():
    """打回三次 ⇒ v1/v2 都在，谁也不被后来的盖掉。

    把版本号写死成 1（或去掉扫号那段循环）⇒ 红（只剩一份，且内容是 v2）。
    """
    from singularity.scheduler.workflow import _save_phase_output
    pid = "1789000000003"
    for v in ("v1", "v2", "v3"):
        _save_phase_output(pid, "architecture.md", v)
    assert [f.read_text(encoding="utf-8") for f in _hist(pid, "architecture.md")] == ["v1", "v2"]


def test_归档塌了正文照样落盘(monkeypatch):
    """归档是尽力而为 —— 它炸了不能把**这一版**也一起丢掉（那才是真丢东西）。"""
    from singularity.scheduler import workflow
    from singularity.scheduler.workflow import _save_phase_output, _phase_output_path
    pid = "1789000000004"
    _save_phase_output(pid, "architecture.md", "v1")

    def boom(*a, **k):
        raise OSError("磁盘满了")
    monkeypatch.setattr(workflow, "_archive_phase_output", boom)
    _save_phase_output(pid, "architecture.md", "v2")

    # 去掉那圈 try/except ⇒ 这里直接抛 OSError，红。
    assert _phase_output_path(pid, "architecture.md").read_text(encoding="utf-8") == "v2"


def test_阶段产出是原子写(monkeypatch):
    """落盘中途出事 ⇒ **上一版还在**，不会留下半截文件（2026-09-19 外派评审 A9）。

    这一族原来是全仓**唯一**一类裸 `write_text` 的状态文件（`project.save()` 和
    `atomic_write_json` 都是 tmp+replace）。后果不是"文件坏了"这么轻：
    半截 JSON 读不出来，而 `handle_gate3_reject` 写的是 `except: has_qa = False`
    ⇒ **"读坏了"和"从来没跑过"在决策侧长得一模一样**，GATE3 打回的路由静默落到 impl。

    判据钉在 `os.replace` 上：**换过去的那一步失败**，原文件必须一个字没变。
    改回裸 `write_text` ⇒ 这里直接抛 OSError 且旧内容已被截断 ⇒ 红。
    """
    from singularity.scheduler import _io
    from singularity.scheduler.workflow import _save_phase_output, _phase_output_path
    pid = "1789000000006"
    _save_phase_output(pid, "qa_report.json", '{"v": 1}')

    def boom(*a, **k):
        raise OSError("磁盘满了")
    monkeypatch.setattr(_io.os, "replace", boom)

    with pytest.raises(OSError):
        _save_phase_output(pid, "qa_report.json", '{"v": 2}')

    assert _phase_output_path(pid, "qa_report.json").read_text(encoding="utf-8") == '{"v": 1}', \
        "写失败了，上一版却被截断 —— 读它的那个消费端只会看到『没有报告』"


def test_history不进项目列表():
    """历史版本待在子目录里 ⇒ `project.list_all()` 的 `glob("*.json")` 扫不到它们。

    如果哪天有人图省事把 history 挪回同级平铺，这条红。
    （同级那份会被 `endswith` 白名单挡不住地漏进来，`list_all` 每轮白读。）
    """
    from singularity.scheduler import project
    from singularity.scheduler.workflow import _save_phase_output
    pid = "1789000000005"
    _save_phase_output(pid, "traceability.json", '{"a": 1}')
    _save_phase_output(pid, "traceability.json", '{"a": 2}')

    flat = {p.name for p in (project._projects_dir()).glob("*.json")}
    assert flat == {f"{pid}.traceability.json"}, f"平铺目录里混进了历史版本: {flat}"

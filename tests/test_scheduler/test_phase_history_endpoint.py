"""阶段产出的**历史版本**要有个只读入口（2026-09-19 补）。

`_save_phase_output` 从 09-17 起就在覆盖前把上一版归档进
`.qidian/projects/<id>/history/<文件名>.<n>` —— **数据早就在盘上，
界面上一点入口都没有**（用户当场想看"打回前后的对比"）。
`research-raw` 那条只解决"这一版看得到全文"，不解决跨版本。

形状照 `research-raw`：只读、纯文本、一个链接就能看。
"""

import pytest

from singularity.scheduler import _api_projects, config
from singularity.scheduler.project import ProjectState, _projects_dir


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    return tmp_path


def _write_history(name: str, bodies: dict[int, str]):
    d = _projects_dir() / "p1" / "history"
    d.mkdir(parents=True, exist_ok=True)
    for n, body in bodies.items():
        (d / f"{name}.{n}").write_text(body, encoding="utf-8")
    return d


def test_把各版拼起来_新在前(_iso):
    _write_history("architecture.md", {1: "第一版", 2: "第二版"})
    text, code = _api_projects.project_phase_history("p1", "architecture.md")
    assert code == 200 and text
    assert "共 2 版" in text
    # 新→旧：想对比时先看到刚被换下来的那版
    assert text.index("第二版") < text.index("第一版")


def test_分隔头带版本号和字节数(_iso):
    _write_history("architecture.md", {3: "内容"})
    text, _ = _api_projects.project_phase_history("p1", "architecture.md")
    assert "版本 3" in text and "字节" in text


def test_没有历史版本时_404(_iso):
    (_projects_dir() / "p1").mkdir(parents=True, exist_ok=True)
    text, code = _api_projects.project_phase_history("p1", "architecture.md")
    assert text is None and code == 404


def test_项目压根不存在时_404_而且不建目录(_iso):
    """⚠️ 别用 `get_project_dir()`（它会 mkdir）—— 查一个不存在的项目
    不该顺手把它的目录建出来（`7bdeb34` 刚修过同族那个坑）。"""
    before = (_projects_dir() / "不存在").exists()
    text, code = _api_projects.project_phase_history("不存在", "architecture.md")
    assert text is None and code == 404
    assert not before and not (_projects_dir() / "不存在").exists(), "查一下就把目录建出来了"


@pytest.mark.parametrize("bad", ["../x", "a/b", "..", "x\\y", ""])
def test_路径穿越的名字一律_400(bad):
    """文件名来自 URL —— 只允许**纯文件名**（同 `task_override_route` 那个形状的边界）。"""
    _write_history("architecture.md", {1: "x"})
    text, code = _api_projects.project_phase_history("p1", bad)
    assert text is None and code == 400, f"{bad!r} 没被拦住"


def test_只收数字后缀的版本(_iso):
    """`history/` 里可能混进别的东西（`architecture.md.bak` 之类）—— 别把它们当版本。"""
    d = _write_history("architecture.md", {1: "真版本"})
    (d / "architecture.md.bak").write_text("不该出现", encoding="utf-8")
    text, _ = _api_projects.project_phase_history("p1", "architecture.md")
    assert "共 1 版" in text and "不该出现" not in text


def test_路由真的挂上了(_iso):
    """**函数对 ≠ 接线通**：handler 写好了、路由忘了注册，界面上那个链接就是 404。"""
    from singularity.web.app import app
    c = app.test_client()
    _write_history("architecture.md", {1: "旧版正文"})
    r = c.get("/api/projects/p1/history/architecture.md")
    assert r.status_code == 200, r.status_code
    assert "旧版正文" in r.get_data(as_text=True)
    assert r.mimetype == "text/plain"
    assert c.get("/api/projects/p1/history/从来没有.md").status_code == 404


def test_一版读不出来时_要说出来而不是当作没有(_iso):
    """**"损坏和没有长得一样"** —— 本仓反复咬人的那个病。"""
    d = _write_history("architecture.md", {1: "好的一版"})
    (d / "architecture.md.2").write_bytes(b"\xff\xfe\x00\x00")   # 不是 UTF-8
    text, code = _api_projects.project_phase_history("p1", "architecture.md")
    assert code == 200
    assert "共 2 版" in text, "坏的那版被静默丢掉了"
    assert "读不出来" in text, "坏了却什么都没说"

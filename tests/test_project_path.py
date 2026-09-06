"""成品保存路径回归：sanitize / repo_dir 用项目名 / create 重名拒绝。"""
from pathlib import Path

import pytest

from singularity.scheduler import config
from singularity.scheduler import project as proj_mod


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_ROOT", tmp_path / "projs")
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / ".qidian")


def test_sanitize_name():
    assert proj_mod._sanitize_name("my-app") == "my-app"
    assert proj_mod._sanitize_name("我的 应用") == "我的-应用"
    assert proj_mod._sanitize_name("a/b\\c") == "a-b-c"
    assert proj_mod._sanitize_name("---") == "project"


def test_repo_dir_uses_name(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    p = proj_mod.create(name="my-app")
    assert proj_mod.repo_dir(p.id) == tmp_path / "projs" / "my-app"


def test_create_duplicate_rejected(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    (tmp_path / "projs" / "my-app").mkdir(parents=True)
    with pytest.raises(ValueError):
        proj_mod.create(name="my-app")


def test_create_duplicate_by_name(tmp_path, monkeypatch):
    """两次 create 同名（sanitize 后相同）→ 第二次拒绝。"""
    _isolate(tmp_path, monkeypatch)
    proj_mod.create(name="my-app")
    with pytest.raises(ValueError):
        proj_mod.create(name="my-app")

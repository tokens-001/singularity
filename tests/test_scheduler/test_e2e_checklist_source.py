"""E2E 清单读的是**哪个仓库的** `test_cases.json`。

`_run_verification` 里 S4 那一段原来读 `config.PROJECT_ROOT / "test_cases.json"`
—— 那是**奇点自己的仓库**，根目录下压根没有 `test_cases.json` ⇒ 这一支永远走不到，
`e2e_checklist.json` **从来没被写出来过**，GATE3 上那份 E2E 清单永远空，
而界面上跟"这个项目本来就没有 E2E 用例"长得一模一样。

⚠️ 同一个错在 `orchestrator._run_integration_check` 里已经修过一遍
（那里留了注释：「原来取 config.PROJECT_ROOT = 奇点自己的仓库」），
所以**两处都要读项目仓库**，别只修一边。

变异验证：把 `_proj_mod.repo_dir(project.id)` 改回 `config.PROJECT_ROOT` → 红。
"""
import json

from singularity.scheduler import config
from singularity.scheduler import project as proj_mod
from singularity.scheduler import workflow


def _mk_project(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / "qidian")
    (tmp_path / "qidian").mkdir(exist_ok=True)
    p = proj_mod.ProjectState(
        id="e2e1", name="E2E 清单来源", raw_constraints=[], owner_confirm={},
        constraints_checklist=[], task_ids=[], issues=[], supervision_log=[],
        lineage=[], handoffs=[], agent_lineup={},
    )
    # ⚠️ **必须有约束**：`_run_verification` 进门就有一句早退
    # （`if not constraints: return [reason]`，为的是别让"验收没得跑"悄悄过去），
    # 而 E2E 那一段在**早退之后**。不给约束的话根本走不到，测试会绿得莫名其妙。
    # 这里给一条**带不了 `check` 的**约束 —— 免得真去跑机械检查。
    p.architecture = {"constraints": [{"type": "security", "rule": "别硬编码密钥"}]}
    proj_mod.save(p)
    return p


def _seed_repo(tmp_path, monkeypatch, cases):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "test_cases.json").write_text(
        json.dumps({"e2e": cases}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(proj_mod, "repo_dir", lambda _id: repo)
    return repo


def test_e2e清单来自项目仓库(tmp_path, monkeypatch):
    """用例写在**项目仓库**里 → 清单必须收得到。"""
    p = _mk_project(tmp_path, monkeypatch)
    _seed_repo(tmp_path, monkeypatch, [{"name": "下单", "user_flow": "a→b",
                                        "success_criteria": "看到订单"}])

    try:
        workflow._run_verification(p, agents={})
    except Exception:
        pass  # 走真验收分支会缺依赖；这里只关心 E2E 那一段

    out = config.QIDIAN_DIR / "projects" / f"{p.id}.e2e_checklist.json"
    assert out.exists(), "E2E 清单没落盘 —— 十有八九又读到奇点自己的仓库去了"
    got = json.loads(out.read_text(encoding="utf-8"))
    assert [c["name"] for c in got] == ["下单"], got


def test_奇点仓库里的同名文件不许被当作用例(tmp_path, monkeypatch):
    """反向：**奇点仓库**下的 `test_cases.json` 与项目无关，不许被读。

    这条钉的是"改回 `config.PROJECT_ROOT` 也照样绿"那种假修法 ——
    所以故意把用例**只**放在奇点仓库里，项目仓库放一份空的。
    """
    p = _mk_project(tmp_path, monkeypatch)
    repo = _seed_repo(tmp_path, monkeypatch, [])          # 项目仓库：空的
    assert repo == tmp_path / "repo"

    fake_engine_root = tmp_path / "singularity"
    (fake_engine_root).mkdir(exist_ok=True)
    (fake_engine_root / "test_cases.json").write_text(
        json.dumps({"e2e": [{"name": "不该出现", "user_flow": "x",
                             "success_criteria": "y"}]}, ensure_ascii=False),
        encoding="utf-8")
    monkeypatch.setattr(config, "PROJECT_ROOT", fake_engine_root)

    try:
        workflow._run_verification(p, agents={})
    except Exception:
        pass

    out = config.QIDIAN_DIR / "projects" / f"{p.id}.e2e_checklist.json"
    assert not out.exists(), "读了奇点自己的仓库 —— 那就是原来那个 bug"

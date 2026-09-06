"""_write_env 最小验证：更新已存在 key + 追加新 key + 注入 os.environ。"""
import os

import singularity.web.app as app


def test_write_env(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("DEEPSEEK_API_KEY=old\nKEEP=1\n")
    monkeypatch.setattr(app, "_ENV_PATH", env)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)

    # 更新已存在的 key，不产生重复行，其他行保留
    app._write_env("DEEPSEEK_API_KEY", "new")
    text = env.read_text()
    assert "DEEPSEEK_API_KEY=new" in text
    assert text.count("DEEPSEEK_API_KEY") == 1
    assert "KEEP=1" in text
    assert os.environ["DEEPSEEK_API_KEY"] == "new"

    # 追加新 key 并注入进程
    app._write_env("ZHIPU_API_KEY", "z")
    assert "ZHIPU_API_KEY=z" in env.read_text()
    assert os.environ["ZHIPU_API_KEY"] == "z"

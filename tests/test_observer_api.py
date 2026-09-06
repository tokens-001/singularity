"""观察者模型标记：_observer 键的存取、以及 add/remove 不覆盖它。"""
from singularity.scheduler import config
from singularity.scheduler import api_store


def test_observer_model_set_get(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / ".qidian")
    assert api_store.get_observer_model() == ""
    api_store.set_observer_model("deepseek-chat")
    assert api_store.get_observer_model() == "deepseek-chat"
    api_store.set_observer_model("")
    assert api_store.get_observer_model() == ""


def test_observer_key_survives_add(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / ".qidian")
    api_store.set_observer_model("deepseek-chat")
    # add 会调 _save（写 api_store.json），_observer 键不能被覆盖丢
    api_store.add("deepseek", "DeepSeek", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY")
    assert api_store.get_observer_model() == "deepseek-chat"

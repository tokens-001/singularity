"""`api_store` 的保留前缀不变量：**只在读的一侧做了，写的一侧没做**。

2026-09-15 真机验证时撞见：`POST /api/api-store {"id": "__probe_gate__"}` 回 200、
写进文件了，然后 ——

  · `GET /api/api-store` **列不出来**（`_load` 的 `startswith("_")` 把它当元数据键跳过）
  · `DELETE /api/api-store/__probe_gate__` 回 `{"ok": false}`（`remove()` 里
    `if api_id not in entries: return False` —— 它压根看不见这个键）

⇒ **建得进、看不见、删不掉**，三条路全断，只能手改 `.qidian/api_store.json`。

这里钉三件事，缺一不可：
  ① 存储层 `add()` 拒绝 —— 它是**唯一那个写入口**，拦在这儿别的调用方也兜得住
  ② HTTP 层回 **400 不是 500**（`_api_admin.api_store_add` 把 ValueError 翻译过去）
  ③ **一个字节都没写进文件** —— "回 400 了但已经写进去了"是更坏的结局
对照：正常 id 照常能建（别把修法改宽成一律拒绝）。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import singularity.web.app as webapp                        # noqa: E402
from singularity.scheduler import api_store, config         # noqa: E402


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """**真隔离**：把 QIDIAN_DIR 指到 tmp —— 不指的话这个用例会往生产
    `.qidian/api_store.json` 里写真东西（本仓栽过，见 §56 同族）。"""
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / ".qidian")
    monkeypatch.setattr(api_store, "_CORRUPT", False)
    return tmp_path / ".qidian" / "api_store.json"


@pytest.fixture
def client(isolated):
    webapp.app.config["TESTING"] = True
    return webapp.app.test_client()


def test_存储层拒绝下划线开头(isolated):
    """① 写入口本身要拦 —— HTTP 那层只是翻译，真正的不变量在这儿。"""
    with pytest.raises(ValueError, match="不能以 _ 开头"):
        api_store.add(api_id="_probe", provider="p", base_url="", api_key_env="")
    assert not isolated.exists(), "回绝了却把文件建出来了"


def test_HTTP端点回400不是500(client, isolated):
    """② 接线：非法 id ⇒ 400（不是 500、不是那个恒 200 的 ok:false）。"""
    r = client.post("/api/api-store", json={"id": "__probe_gate__"})
    assert r.status_code == 400, f"应 400，实际 {r.status_code}: {r.get_data(as_text=True)[:200]}"
    assert r.is_json and r.get_json().get("error")


def test_回400时一个字节都没落盘(client, isolated):
    """③ **最要紧的那条**：回绝必须是干净的。

    只钉状态码是不够的 —— "先写进去、再报 400" 同样能把条目搞丢，
    而调用方只会看到 400、以为没事。所以直接查文件。
    """
    before = isolated.read_text(encoding="utf-8") if isolated.exists() else None
    r = client.post("/api/api-store", json={"id": "__probe_gate__"})
    assert r.status_code == 400
    after = isolated.read_text(encoding="utf-8") if isolated.exists() else None
    assert after == before, f"回绝了却动了文件：\n{before!r}\n→\n{after!r}"


def test_对照_正常id照常能建(client, isolated):
    """对照：**别把修法改宽**成"凡 POST 都拒"。"""
    r = client.post("/api/api-store", json={"id": "probe_normal", "provider": "probe_normal"})
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    assert "probe_normal" in api_store.list_all(), "正常 id 应该建得进去"

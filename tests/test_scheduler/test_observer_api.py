

class TestResearchRawRoute:
    """调研报告**全文**的出口 —— 2026-09-17 真机，用户原话「我怎么不能看报告」。

    模型吐的 JSON 坏了 ⇒ `try_parse_json` 走兜底（只留前 5000 字）⇒ 前端渲染成空框；
    而**全文一直躺在 `<id>.research.md` 里，界面上没有任何入口**
    （`/api/projects/<id>/files` 那条路会跳过 `.md` 和 `.qidian/`）。
    """

    def _client(self):
        from singularity.web.app import app
        return app.test_client()

    def test_没有原文时_404_而不是空字符串(self, monkeypatch, tmp_path):
        """**"没有"必须和"空"分得开** —— 回 `""` 会让界面显示成"报告是空的"。"""
        from singularity.scheduler import config
        monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
        r = self._client().get("/api/projects/nope/research-raw")
        assert r.status_code == 404, f"没有原文却回了 {r.status_code}"

    def test_有原文时_原样端出来(self, monkeypatch, tmp_path):
        from singularity.scheduler import config
        monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
        d = tmp_path / "projects"
        d.mkdir(parents=True, exist_ok=True)
        (d / "p1.research.md").write_text("```json\n{\"坏\": \"带\n换行\"}\n```", encoding="utf-8")
        r = self._client().get("/api/projects/p1/research-raw")
        assert r.status_code == 200, r.status_code
        body = r.get_data(as_text=True)
        assert "坏" in body and body.count("\n") >= 2, f"原文没原样端出来: {body!r}"

"""codegraph.py 单元测试 — query() 纯函数。"""

import pytest


class TestQuery:
    @staticmethod
    def _make_graph():
        return {
            "import_graph": {
                "app": ["os", "json"],
                "utils": ["math", "app"],
            },
            "files": {
                "src/app.py": {"classes": ["App", "Config"], "functions": ["main", "init"]},
                "src/utils.py": {"classes": ["Helper"], "functions": ["format", "parse"]},
            },
        }

    def test_query_imports_module_match(self):
        from singularity.scheduler.codegraph import query
        r = query(self._make_graph(), "imports", "app")
        assert len(r) >= 1
        assert any("app" in x for x in r)

    def test_query_imports_import_match(self):
        from singularity.scheduler.codegraph import query
        r = query(self._make_graph(), "imports", "json")
        assert len(r) >= 1

    def test_query_imports_no_match(self):
        from singularity.scheduler.codegraph import query
        r = query(self._make_graph(), "imports", "nonexistent")
        assert r == []

    def test_query_file(self):
        from singularity.scheduler.codegraph import query
        r = query(self._make_graph(), "file", "app.py")
        assert len(r) >= 1
        assert "App" in r[0] or "Config" in r[0]

    def test_query_file_no_match(self):
        from singularity.scheduler.codegraph import query
        r = query(self._make_graph(), "file", "nonexistent.py")
        assert r == []

    def test_query_classes(self):
        from singularity.scheduler.codegraph import query
        r = query(self._make_graph(), "classes", "Helper")
        assert len(r) >= 1
        assert "utils.py" in r[0]

    def test_query_classes_case_insensitive(self):
        from singularity.scheduler.codegraph import query
        r = query(self._make_graph(), "classes", "helper")
        assert len(r) >= 1

    def test_query_classes_no_match(self):
        from singularity.scheduler.codegraph import query
        r = query(self._make_graph(), "classes", "NotExist")
        assert r == []

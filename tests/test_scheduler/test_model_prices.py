"""model_prices.py 单元测试 — 单价读写 + 脏数据防御。

这一层是"金额不再编造"的地基：查不到必须返回 None，任何情况下都不能凭空给一个数。
"""

import json

import pytest

from singularity.scheduler import config, model_prices


def _write_raw(text: str):
    (config.QIDIAN_DIR / "model_prices.json").write_text(text, encoding="utf-8")


class TestReadWrite:
    def test_unknown_model_is_none_not_default(self):
        # 最关键的一条：查不到就是 None。旧代码这里返回 0.50 兜底 —— 那就是编造金额的源头。
        assert model_prices.price_for("never-configured") is None

    def test_roundtrip(self):
        model_prices.set_price("m1", 0.14)
        assert model_prices.price_for("m1") == 0.14
        assert model_prices.load_prices() == {"m1": 0.14}

    def test_zero_and_none_delete_key(self):
        model_prices.set_price("m1", 0.14)
        model_prices.set_price("m1", 0)
        assert model_prices.price_for("m1") is None
        model_prices.set_price("m2", 0.14)
        model_prices.set_price("m2", None)
        assert model_prices.price_for("m2") is None

    def test_negative_deletes_instead_of_writing(self):
        model_prices.set_price("m1", 0.14)
        model_prices.set_price("m1", -1.0)
        assert model_prices.price_for("m1") is None

    def test_setting_one_model_keeps_others(self):
        model_prices.set_price("a", 0.1)
        model_prices.set_price("b", 0.2)
        model_prices.set_price("c", 0.3)
        assert model_prices.load_prices() == {"a": 0.1, "b": 0.2, "c": 0.3}


class TestDirtyData:
    def test_missing_file(self):
        assert model_prices.load_prices() == {}

    def test_malformed_json_does_not_raise(self):
        _write_raw("{ this is not json")
        assert model_prices.load_prices() == {}

    def test_non_dict_json(self):
        _write_raw('["a", "b"]')
        assert model_prices.load_prices() == {}

    @pytest.mark.parametrize("bad", ['"0.14"', "-1", "0", "null", "true", "false"])
    def test_dirty_values_dropped(self, bad):
        _write_raw('{"good": 0.14, "bad": %s}' % bad)
        assert model_prices.load_prices() == {"good": 0.14}

    def test_nan_and_infinity_dropped(self):
        # NaN 单价会让整张用量表变成 NaN；必须挡住，不能传播出去
        _write_raw('{"good": 0.14, "nan": NaN, "inf": Infinity}')
        assert model_prices.load_prices() == {"good": 0.14}

    def test_bool_is_not_a_price(self):
        # Python 里 isinstance(True, int) 为真 —— 不显式排除就会变成单价 1.0
        _write_raw('{"flag": true, "good": 0.14}')
        assert model_prices.load_prices() == {"good": 0.14}

    def test_numeric_string_is_dropped_not_coerced(self):
        # 手改文件写成字符串属于配置错误，宁可当没配，也不要猜用户的意思
        _write_raw('{"m": "0.14"}')
        assert model_prices.load_prices() == {}


class TestSurvivesModelTableRewrites:
    """价格存独立文件的核心收益：模型表被整行重建也擦不掉它。

    `_benchmark.py` 跑基准、`models_import` 扫描导入，都会把 ModelEntry 每个字段
    重新传一遍。价格若存在模型表里，这两条路径会把它静默归零。
    """

    def test_price_survives_benchmark_style_rewrite(self):
        from singularity.scheduler import model_registry
        model_prices.set_price("m1", 0.14)

        # add_model 的调用形状照抄 _benchmark.py:158-163
        model_registry.add_model(
            "m1", "deepseek", "M1", ["any"],
            speed="fast", cost="standard", rating="S",
            reasoning=False, max_turns=6, notes="benchmarked", strengths=[],
        )

        assert model_prices.price_for("m1") == 0.14

    def test_price_survives_custom_model_resave(self):
        from singularity.scheduler import api_store
        model_prices.set_price("m1", 0.14)

        # 调用形状照抄 _api_admin.models_import:131-138
        api_store.save_custom_model("m1", provider="deepseek", display="M1")

        assert model_prices.price_for("m1") == 0.14


class TestPriceEndpointHandler:
    """PUT /api/model-price/<id> 的校验。

    这条路径**故意不照抄** PUT /api/models/<id>（那条零校验）：一个 NaN 单价会让
    整张用量表变成 NaN，负数会凭空抵消真实花费，而"价格没配好"在界面上表现为
    一个看着正常的错误数字 —— 正是本次要消灭的东西。
    """

    def _set(self, model_id, payload):
        from singularity.scheduler._api_admin import model_price_set
        return model_price_set(model_id, payload)

    @pytest.mark.parametrize("bad", ["abc", "", " ", "1,5", [], {}])
    def test_non_numeric_rejected(self, bad):
        _, code = self._set("m", {"price_per_m": bad})
        if bad in ("", " "):     # 空串 = 清除，是合法输入
            assert code == 200
        else:
            assert code == 400, f"{bad!r} 应当被拒"
        assert model_prices.price_for("m") is None

    @pytest.mark.parametrize("bad", [-1, -0.01, 1001, 99999])
    def test_out_of_range_rejected(self, bad):
        _, code = self._set("m", {"price_per_m": bad})
        assert code == 400
        assert model_prices.price_for("m") is None

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_rejected(self, bad):
        _, code = self._set("m", {"price_per_m": bad})
        assert code == 400
        assert model_prices.price_for("m") is None

    def test_valid_price_accepted(self):
        body, code = self._set("m", {"price_per_m": 0.14})
        assert code == 200
        assert body["price_per_m"] == 0.14
        assert model_prices.price_for("m") == 0.14

    def test_accepts_numeric_string(self):
        # 表单/手输常见形态，这里允许（与文件读取的严格不同：文件是人类手改的配置）
        _, code = self._set("m", {"price_per_m": "0.14"})
        assert code == 200
        assert model_prices.price_for("m") == 0.14

    def test_null_clears(self):
        model_prices.set_price("m", 0.14)
        body, code = self._set("m", {"price_per_m": None})
        assert code == 200
        assert body["price_per_m"] is None
        assert model_prices.price_for("m") is None

    def test_zero_clears_not_free(self):
        model_prices.set_price("m", 0.14)
        body, code = self._set("m", {"price_per_m": 0})
        assert code == 200
        assert model_prices.price_for("m") is None, "0 是'未配置'，不是'免费'"

    def test_missing_model_id_rejected(self):
        _, code = self._set("", {"price_per_m": 0.14})
        assert code == 400


class TestModelListExposesPrice:
    """/api/models 要带上单价，且未配置时是 None（不是 0）。"""

    def _seed_custom(self, *mids):
        """model_list 只列出 models_custom 里的模型 —— 不先塞进去就是空跑。"""
        from singularity.scheduler import api_store
        for mid in mids:
            api_store.save_custom_model(mid, provider="deepseek", display=mid)

    def test_priced_and_unpriced_both_exposed(self):
        from singularity.scheduler._api_admin import model_list
        self._seed_custom("priced", "unpriced")
        model_prices.set_price("priced", 0.14)

        body, code = model_list()
        assert code == 200
        assert {"priced", "unpriced"} <= set(body), "模型没进目录，下面的断言会空跑"
        assert body["priced"]["price_per_m"] == 0.14
        assert body["unpriced"]["price_per_m"] is None, "未配置必须是 None，不能是 0"

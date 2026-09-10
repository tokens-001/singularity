"""model_prices.py — 每个模型的单价（唯一数据源）。

存 `.qidian/model_prices.json`，就是一张平表: ``{"deepseek-v4-flash": 0.14}``。

**单位：USD / 百万 token，混合价。** 为什么是"混合"：``UsageRecord`` 只记总 token 数，
不区分输入/输出，所以系统能用的只有单一混合单价。想按输入/输出分别计价，
得先改执行器的上报路径 —— 那之前，拆成两个价格字段是建了存储没数据填。

**为什么不塞进模型表**（models_custom.json / ModelEntry）：`_benchmark.py` 与
`models_import` 会把模型条目**整行重建**（每个字段重新传一遍）。价格若存在行里，
用户点一次「跑基准」或做一次「扫描导入」，手填的价就静默归零了。分开存让这种
失败在结构上不可能发生 —— 那些路径根本不碰这个文件。

⚠️ **绝不引入默认单价。** 这里就是旧 `_token_budget._estimate_cost` 那个
``rates.get(model, 0.50)`` 的坟：一个查不到就按 $0.50 算的静默兜底，
让界面上显示的金额全是编的。查不到就返回 None，由调用方如实显示"未配置价格"。
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from singularity.scheduler import config
from singularity.scheduler._io import atomic_write_json


def _path() -> Path:
    return config.QIDIAN_DIR / "model_prices.json"


def load_prices() -> dict[str, float]:
    """读全部单价。文件缺失/损坏一律返回空 dict，不抛。

    丢弃脏条目（非数字 / 非有限数 / <= 0）而不是让它们传播：
    一个 NaN 单价会让整张用量表变成 NaN，一个负数会凭空抵消真实花费。
    注意排除 bool —— Python 里 ``isinstance(True, int)`` 为真，True 会变成 1.0。
    """
    p = _path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in data.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        f = float(v)
        if math.isfinite(f) and f > 0:
            out[str(k)] = f
    return out


def price_for(model: str) -> float | None:
    """该模型的单价（USD/百万 token）。**未配置返回 None，绝不兜底。**"""
    return load_prices().get(model)


def all_prices() -> dict[str, float]:
    return load_prices()


def set_price(model: str, price: float | None) -> None:
    """设置单价；``None`` / ``0`` / 负数 = **删除该键**（未配置，不是免费）。

    空值走删除而不是写入 0：0 是一个合法的"这个模型不要钱"，而"没填"和"免费"
    是两回事 —— 混在一起就再也分不出"我没填"和"它真免费"了。
    """
    prices = load_prices()
    if price is None:
        prices.pop(model, None)
    else:
        p = float(price)
        if not math.isfinite(p) or p <= 0:
            prices.pop(model, None)
        else:
            prices[model] = p
    atomic_write_json(_path(), prices)

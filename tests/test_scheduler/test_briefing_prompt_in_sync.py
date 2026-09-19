"""外发提示词和位置说明**不许各说各话** —— 它们会被一起发出去。

来历（2026-09-18）：那份材料的判断 A/B/D 改对了，**给外部的提示词没跟上**，
而提示词当时**只活在对话里、盘上没有** —— 它不漂给任何人看，发出去就是自相矛盾，
事后也查不到当时到底发了什么。09-19 落盘成 `docs/外派提示词-*.md` 并加这台守卫。

判据：两份文件的 §0（硬约束）和 §7（请答什么）**逐字相同**（比 body，不比标题）。
⚠️ **比正文之间要先归一空白**：Markdown 里一个换行的差别不该算"不同步"，
否则守卫会天天红、然后被人调松 —— 那比没有它更坏。
"""

from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parents[2] / "docs"
BRIEFING_GLOB = "外派提示词-*.md"

#: 必须逐字一致的两节（前缀匹配，标题里的破折号/补充说明允许不同）
SECTIONS = ("## 0. ", "## 7. ")


def _briefing() -> Path:
    files = sorted(DOCS.glob(BRIEFING_GLOB))
    assert files, f"找不到 {BRIEFING_GLOB} —— 提示词又被塞回对话里了？"
    return files[-1]


def _position_doc() -> Path:
    """位置说明带日期，取最新那份。"""
    files = sorted(DOCS.glob("奇点位置说明-*.md"))
    assert files, "找不到位置说明"
    return files[-1]


def _section_body(text: str, prefix: str) -> str:
    """取 `## <prefix>` 那节到下一个 `## ` 之间的正文，空白归一后返回。"""
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith(prefix)), None)
    assert start is not None, f"这份文件里没有 {prefix!r} 这一节"
    body = []
    for ln in lines[start + 1:]:
        if ln.startswith("## "):
            break
        # 分隔线不算内容 —— 提示词里为了让"从哪开始复制"看得见，多插了几条
        if ln.strip() in ("---", "***"):
            continue
        body.append(ln.rstrip())
    return "\n".join(ln for ln in body if ln.strip())


@pytest.mark.parametrize("prefix", SECTIONS)
def test_prompt_section_matches_the_position_doc(prefix):
    a = _section_body(_briefing().read_text(encoding="utf-8"), prefix)
    b = _section_body(_position_doc().read_text(encoding="utf-8"), prefix)
    assert a == b, (
        f"{prefix!r} 这两份对不上了 —— 一起发出去就是自相矛盾。\n"
        f"  提示词：{_briefing().name}\n  材料：{_position_doc().name}\n"
        f"改哪份都行，**另一份要跟上**。")


def test_guard_actually_has_content_to_compare():
    """**扫不到东西的守卫比没有更坏** —— 眼看绿，其实什么都没查。

    抄 `test_no_silent_except.py` 的自检：两份都得真取到一段正文，
    否则上面那条 `==` 会在"两边都空"时恒真。
    """
    for prefix in SECTIONS:
        for p in (_briefing(), _position_doc()):
            body = _section_body(p.read_text(encoding="utf-8"), prefix)
            assert len(body) > 50, f"{p.name} 的 {prefix!r} 只取到 {len(body)} 字，判据可能坏了"

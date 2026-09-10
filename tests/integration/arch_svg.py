"""把 arch_graph.py 的 import 依赖图渲染成自包含 HTML。

**为什么是矩阵而不是节点连线图**：这张图 12 个组 / 54 条跨组边，而且有一根
「万物皆连」的枢纽（infra 入边 190）。节点连线图在这种密度下必然是一团毛球
（我第一版就是，被用户直接说"乱七八糟"）—— 线越多越读不出东西。
稠密图的正确答案是**邻接矩阵**：每个格子只表达一件事，没有连线交叉，
行列可以排序让相关的东西挨在一起。

矩阵下面配一张**只留最强边**的带状图，用来一眼看出主干方向。

用法: .venv/bin/python tests/integration/arch_svg.py <graph.json> <out.html>
"""
import json
import sys
from pathlib import Path

CELL, LABEL_W, TOP_H = 62, 150, 150
TOP_EDGES = 12


def main() -> int:
    data = json.loads(Path(sys.argv[1]).read_text())
    labels: dict = data["labels"]
    sizes: dict = data["sizes"]
    edges: dict[tuple[str, str], int] = {}
    for k, n in data["edges"].items():
        a, _, b = k.partition("->")
        edges[(a, b)] = n

    # 排序：总交互量大的靠前 → 相关的东西挨在一起，矩阵更成块
    groups = sorted(sizes,
                    key=lambda g: -(sum(n for (a, b), n in edges.items() if a == g or b == g)))
    mx = max(edges.values()) if edges else 1
    n = len(groups)
    W = LABEL_W + CELL * n + 40
    H = TOP_H + CELL * n + 60

    def shade(v: int) -> str:
        if not v:
            return "#141a21"
        r = v / mx
        # 暗蓝 → 亮蓝，对数感更均匀
        return f"rgb({int(30 + 90 * r)},{int(70 + 110 * r)},{int(120 + 120 * r)})"

    cells = []
    for i, g in enumerate(groups):
        for j, h in enumerate(groups):
            v = edges.get((g, h), 0)
            x = LABEL_W + j * CELL
            y = TOP_H + i * CELL
            cells.append(
                f'<rect x="{x}" y="{y}" width="{CELL-2}" height="{CELL-2}" rx="3" fill="{shade(v)}"/>')
            if v:
                # 用亮度决定字色，保证读得清
                light = v / mx > 0.55
                cells.append(
                    f'<text x="{x+CELL/2-1:.0f}" y="{y+CELL/2+5:.0f}" class="cv" '
                    f'style="fill:{"#0f1419" if light else "#cfe0f2"}">{v}</text>')

    # 行列标签
    ticks = []
    for i, g in enumerate(groups):
        y = TOP_H + i * CELL + CELL / 2
        ticks.append(f'<text x="{LABEL_W-10}" y="{y+5:.0f}" class="rl">{labels.get(g,g)}'
                     f'<tspan class="dim"> {sizes.get(g,0)}文件</tspan></text>')
    for j, g in enumerate(groups):
        x = LABEL_W + j * CELL + CELL / 2 - 1
        ticks.append(f'<text x="{x:.0f}" y="{TOP_H-12}" class="cl" '
                     f'transform="rotate(-45 {x:.0f} {TOP_H-12})">{labels.get(g,g)}</text>')

    # 主干带状图：只留最强边，横向条
    top = sorted(edges.items(), key=lambda kv: -kv[1])[:TOP_EDGES]
    bars = []
    bx, by, bw = LABEL_W, 40, 320
    for k, ((a, b), v) in enumerate(top):
        y = by + k * 7 + 4
        ln = bw * v / top[0][1]
        bars.append(f'<text x="{bx-10}" y="{y+4}" class="bl">{labels.get(a,a)} → {labels.get(b,b)}</text>')
        bars.append(f'<rect x="{bx+bw+10}" y="{y-2}" width="{ln:.0f}" height="5" rx="2" fill="#3d7fd0"/>')
        bars.append(f'<text x="{bx+bw+18+ln:.0f}" y="{y+4}" class="bv">{v}</text>')
    bar_h = len(top) * 7 + 10

    html = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>奇点 — 模块耦合矩阵</title>
<style>
 body{{margin:0;background:#0f1419;color:#dde3ea;font-family:-apple-system,"PingFang SC",sans-serif}}
 h1{{font-size:17px;margin:18px 24px 3px;font-weight:600}}
 h2{{font-size:13px;margin:22px 24px 6px;color:#9fb0c4;font-weight:600}}
 .sub{{font-size:12px;color:#8a97a6;margin:0 24px;line-height:1.75}}
 svg{{display:block;margin:6px 24px}}
 .cv{{font-size:12px;text-anchor:middle;font-weight:600}}
 .rl{{fill:#c3d2e2;font-size:11px;text-anchor:end}}
 .cl{{fill:#9fb0c4;font-size:10px;text-anchor:start}}
 .dim{{fill:#5f7183;font-size:9px}}
 .bl{{fill:#c3d2e2;font-size:10px;text-anchor:end}}
 .bv{{fill:#7f92a6;font-size:10px}}
</style>
<h1>奇点 — 模块耦合矩阵（边来自 ast 解析）</h1>
<p class="sub">
 行 = 依赖方，列 = 被依赖方，格子里的数字 = 跨组 import 次数（空格 = 无依赖）。
 组按总交互量排序，所以相关的东西挨在一起。
 <b>分组规则是我的判断</b>（按文件名前缀归并），<b>数字是代码里真实的 import</b>。<br>
 注意 <code>模型/基础设施</code> 那一<b>列</b>——几乎所有行都有值，说明它是被所有人依赖的底座。
</p>

<h2>① 主干方向（最强 {TOP_EDGES} 条）</h2>
<svg width="{W}" height="{bar_h}">{''.join(bars)}</svg>

<h2>② 完整矩阵</h2>
<svg width="{W}" height="{H}">{''.join(ticks)}{''.join(cells)}</svg>
</html>"""
    Path(sys.argv[2]).write_text(html, encoding="utf-8")
    print(f"矩阵 {n}×{n} / {len(edges)} 条边 / 主干 {len(top)} 条 → {sys.argv[2]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

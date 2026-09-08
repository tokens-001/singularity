---
name: archify
description: 架构可视化——把架构方案转成交互式架构图（组件/边界/连线/数据流）
type: prompt
---

# Archify 架构图

把架构方案渲染成交互式 HTML 架构图。渲染器是零依赖 Node CLI，路径：

```
$SKILL_DIR/vendor/bin/archify.mjs
```

## 命令

```bash
# 渲染（JSON → HTML）
node $SKILL_DIR/vendor/bin/archify.mjs render architecture <in.json> <out.html>

# 验证（布局/连线/标签检查，迭代到 ok:true）
node $SKILL_DIR/vendor/bin/archify.mjs validate architecture <in.json> --json

# 架构改版对比（Before/Delta/After）
node $SKILL_DIR/vendor/bin/archify.mjs compare architecture <base.json> <head.json> <diff.html>
```

## 什么时候用

- 架构阶段产出方案后，把方案画成架构图给人看
- 架构改版/重构时，用 compare 标出增删改的组件

## JSON 结构（architecture 类型）

```json
{
  "schema_version": 1,
  "diagram_type": "architecture",
  "meta": { "title": "标题", "output": "xxx.html", "quality_profile": "standard" },
  "components": [
    { "id": "web", "type": "backend", "label": "Flask API", "sublabel": "说明", "pos": [470, 130], "size": [160, 60] }
  ],
  "boundaries": [
    { "kind": "region", "label": "分组名", "wraps": ["web", "api"] }
  ],
  "connections": [
    { "id": "a-b", "from": "web", "to": "api", "label": "HTTP", "variant": "emphasis" }
  ],
  "cards": [
    { "dot": "cyan", "title": "卡片标题", "items": ["要点1", "要点2"] }
  ]
}
```

## 关键规则

- **component type 枚举**：`frontend` / `backend` / `database` / `cloud` / `security` / `messagebus` / `external`
- **connection variant**：`default` / `emphasis` / `security` / `dashed`
- **每个组件必须写 pos（坐标）和 size（尺寸）**，连线写 from/to
- **垂直/水平对齐**：一条直线上的组件，中心 x（或 y）要对齐，否则自动路由会把直线误判成斜线
- **连线标签别压在组件上**：报 label overlap 时给连线加 `"labelDy": 24`

## 迭代规则（必须做）

产 JSON 后**必须跑 validate 迭代到 ok:true**，别一次就 render：

1. 写 JSON → `node ... validate ... --json`
2. 报错 → 按报错的 `Suggested fix` 改（对齐中心 / 加 labelDy / 加 fromSide+toSide）
3. 再 validate，直到 `"ok": true`
4. 最后 render 出 HTML

完整字段以 `$SKILL_DIR/vendor/schemas/architecture.schema.json` 为准。不确定字段时先读 schema，别编字段。

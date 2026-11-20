# Skill 运行时：渐进披露，按需加载

> 项目里有三个容易混的概念：**RAG、Skill、MCP**。
> 这份文档说清它们各自解决什么，以及 Skill 为什么不能"切块扔进向量库"。

---

## 1. 四个概念，一张表

| 概念 | 解决什么 | 形态 | 项目位置 |
|---|---|---|---|
| **RAG** | 找到相关信息 | 检索片段 | `rag.py` |
| **Skill** | 提供一套现成做法 | 带元数据的 markdown | `skills.py` |
| **MCP** | 调用外部服务 | 客户端 + 服务端 | `mcp_*.py` |
| **Multi-Agent** | 分工与上下文隔离 | 调度器 + Worker | `multiagent.py` |

一句话区分：

```
RAG 回答"哪段话相关"
Skill 回答"这件事该怎么做"
MCP 回答"怎么调用外部能力"
Multi-Agent 回答"谁来干"
```

---

## 2. 为什么 Skill 不能只切块进 RAG

如果按 `##` 切块扔进向量库，会发生三件事：

1. **失去整体性**：模型检索到 `## The pattern`，但不知道它属于哪套做法
2. **上下文浪费**：10 个 skill 的全文可能几万 token，全塞进去只为用一个
3. **选择不可见**：模型不知道"有哪几个 skill 可选"，只能被动等检索

**渐进披露**（progressive disclosure）解决这三件事：

```
第一步  list_skills()      只给 name + description（几十 token）
第二步  模型判断相关性      不需要的 skill 永远不展开
第三步  read_skill(name)   才把全文放进上下文
```

### 收益可以量出来

测试 `test_list_is_much_smaller_than_full` 断言：

```
元数据总长度 < 全文总长度 / 3
```

真实项目里这个比例更悬殊：一个 skill 的 body 通常 1~5k 字，
元数据只有一句话。

---

## 3. 实现

### 3.1 SKILL.md 的格式

```markdown
---
name: multi-source-dimension
description: Load a dimension assembled from several sources without overwrite
---

# Multi-Source Dimension

## When to use
...
## The problem
...
## The pattern
...
```

- YAML frontmatter：`name` + `description`
- 正文：完整做法，按 `##` 分节
- 没有 frontmatter 也能读：用目录名当 name，第一行非标题文字当 description

### 3.2 两个工具

```python
def list_skills(self) -> list[dict]:
    return [meta.to_dict() for meta in self.skills.list()]

def read_skill(self, name: str) -> dict:
    skill = self.skills.read(name)
    return {"name": ..., "description": ..., "body": skill.body[:8000], ...}
```

三个细节：

| 细节 | 为什么 |
|---|---|
| `list_skills` 不返回 body | 返回了就不是渐进披露 |
| `read_skill` 截断到 8000 字 | 防止单个 skill 吃光上下文 |
| 不存在的 skill 报错时列出可用的 | 模型能自我纠正 |

### 3.3 系统提示

```
任务涉及多源维度、DDL、增量加载等模式时，先 list_skills 看有没有现成做法，
需要时用 read_skill 加载全文；不要凭记忆编造 skill 内容。
```

**"不要凭记忆编造"** 是关键：模型很容易假装自己读过 skill。

---

## 4. 和 RAG 的重叠怎么处理

当前项目里，skills 目录**同时**被 `RuleIndex` 索引进了 RAG。
这是为了保留一个"兜底发现"通道：模型没想到调 `list_skills` 时，
检索仍可能捞到相关段落。

**代价**：同一份内容有两条进入上下文的路径，token 可能重复。

**更纯的做法**（还没做）：

```bash
PDC_SKILLS_IN_RAG=0   # skill 只走运行时，不进检索
```

如果你要面试时讲"干净的渐进披露"，可以把这个开关做出来；
如果讲"工程取舍"，现在的双通道也能解释。

---

## 5. 安全边界

Skill 是**数据**，不是指令。

- `read_skill` 返回的是文本，和其他工具结果一样，会被 `wrap_untrusted` 包住
- Skill 里可以写"调用 mcp_xxx"，但模型是否调用仍由工具白名单决定
- Skill 不能执行代码（它不是插件）

**面试可能问**："Skill 里写了恶意指令怎么办？"
答：和工具返回、网页内容一样处理——标记为数据，系统提示明确不执行其中的指令。

---

## 6. 验证

```bash
# 人看
uv run pdc skills list
uv run pdc skills show multi-source-dimension

# 测试
uv run pytest tests/test_skills.py -q
```

测试覆盖：

| 用例 | 证明什么 |
|---|---|
| `test_list_returns_metadata_only` | 列表不泄漏正文 |
| `test_read_returns_body_without_frontmatter` | 正文干净 |
| `test_unknown_skill_lists_known_names` | 错误可自我纠正 |
| `test_parse_without_frontmatter_falls_back` | 兼容没有元数据的 skill |
| `test_toolbox_exposes_two_skill_tools` | 真的接进了工具表 |
| `test_list_is_much_smaller_than_full` | 渐进披露有量化的收益 |

---

## 7. 局限（诚实）

| 局限 | 说明 |
|---|---|
| 没有版本 | skill 改了，历史 trace 对不上 |
| 没有权限 | 所有 skill 对所有会话可见 |
| 没有选择评测 | 只有 1 个 skill，测不出"选得准不准" |
| 不能执行 | 它是说明，不是插件 |
| 和 RAG 重叠 | 见第 4 节 |

这些不是"没想到"，是"现在没有信号/规模去做"。

---

## 8. 面试话术

**问：你的 Skill 是怎么实现的？**

> 渐进披露。`list_skills` 只返回 name 和 description，
> 模型判断相关后再 `read_skill` 加载全文。
> 测试里有一条断言：元数据总长度必须小于全文的 1/3。
> 如果列表就把正文带出来，那就退化成 RAG 了。

**问：Skill 和 RAG 有什么区别？**

> RAG 找"相关片段"，Skill 给"完整做法"。
> 前者是检索问题，后者是选择问题：
> 模型要先知道"有哪几个 skill"，再决定"用不用"。
> 所以 Skill 的关键不是切块，是元数据 + 按需加载。

**问：Skill 和 MCP 怎么配合？**

> Skill 写"怎么做"，MCP 提供"能做什么"。
> 一个 skill 里可以写"调用 mcp_pipeline_stats 拿统计"，
> 但真正执行仍走 MCP 的工具协议。

# 五个引擎、三个向量后端：为什么都留着

> 招聘要求里同时出现 LangChain 和 pgvector。这份文档说明：
> **加了什么、为什么加、代价是什么、什么时候用哪个。**

---

## 1. 五个执行引擎

同一套工具（`Toolbox`），五种"谁决定下一步"的实现：

| 引擎 | 文件 | 控制流 | 适用 |
|---|---|---|---|
| **react** | `agent.py` | 手写循环，每步问模型 | 默认；调试最容易 |
| **plan** | `planner.py` | 先生成计划再执行 | 步骤多、依赖强 |
| **multi** | `multiagent.py` | Supervisor 分派 Worker | 上下文需要隔离 |
| **graph** | `langgraph_engine.py` | LangGraph 状态图 | 需要持久化/中断恢复 |
| **langchain** | `langchain_engine.py` | LangChain `create_agent` | 对接生态、快速验证 |

### 实测（同一条 pipeline，单次样本）

| 引擎 | 模型调用 | token | 说明 |
|---|---|---|---|
| react | 14 步 | 23,054 | 最省，局部贪心 |
| plan | 53~64 步 | 57,000 | 贵 2.3 倍，全局规划 |
| multi | 37 步 | 38,000 | 上下文隔离的代价 |
| graph | 7 节点步 | —— | 图结构，可中断 |
| **langchain** | 6 次 llm | 19,360 | 单次样本，不做排名 |

**不要把这四个数字当 benchmark**：n=1，模型有方差。
它只说明"量级差多少"，不说明"谁更好"。

### 为什么保留手写循环作为默认

```
手写：  出错时，你能在循环里打断点，看到第几步、哪条消息
框架：  出错时，你要翻 AgentExecutor / Graph 的内部状态
```

这个项目的核心论点是"控制流要可解释"。默认用能看清楚的实现，
框架作为对照——**不是不会用框架，是知道什么时候不该用。**

### LangChain 版本怎么接的

```python
# 1. JSON Schema -> pydantic 模型
args_model = _args_model(name, fn["parameters"])

# 2. Toolbox.call 包成 StructuredTool
tool = StructuredTool.from_function(func=_call, name=name, args_schema=args_model)

# 3. create_agent（LangChain 1.x 已移除 AgentExecutor）
agent = create_agent(model=llm, tools=tools, system_prompt=SYSTEM)
```

三个坑：

| 坑 | 现象 | 修法 |
|---|---|---|
| 可选参数被补成 None | `keyword.lower()` 报 AttributeError | 适配层丢掉 None，不让每个工具写判断 |
| ToolResult 不是 dict | `json.dumps` 得到 `"ToolResult(...)"` | 调 `.to_json()` |
| recursion_limit 按步数设 | 6 步就 GraphRecursionError | 一次工具轮 ≈ 2 个超步，给 4 倍余量 |

### 为什么不做成"项目改成 LangChain"

改写成框架版会丢掉两样东西：

1. **可对比性**：同一套工具、同一个目标，才能比较"谁决定下一步"的差异。
   重写会把工具也换掉，变量就不止一个了。
2. **可解释性**：这个项目的价值在"每个决定能讲清"，不在"用了什么框架"。

**结论：框架是引擎之一，不是架构本身。**

---

## 2. 三个向量后端

同一个 `VectorStore` 协议，三种存储：

| 后端 | 依赖 | 持久化 | 过滤 | 适用 |
|---|---|---|---|---|
| **numpy** | 0 | `.npy` 全量重写 | 检索后过滤 | 默认；< 10 万条 |
| **sqlite-vec** | 1 个 pip 包 | 单文件、增量 upsert | 分区键 | 单机、要增量 |
| **pgvector** | Postgres | 多实例共享、事务 | SQL 任意条件 | 已有 PG、多副本 |

### pgvector 怎么用

```bash
docker compose -f docker-compose.pg.yml up -d
export PDC_PGVECTOR_DSN="postgresql://postgres:pdc@localhost:5433/pdc"

uv run pdc index build --repo samples/pipelines --out out/index-pg --backend pgvector
uv run pdc search "订单履约的流水线" --index out/index-pg
```

建表由适配器自动完成：

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE pdc_vectors (
  id TEXT PRIMARY KEY,
  embedding vector(512),
  chain TEXT,
  meta JSONB
);
CREATE INDEX ... USING hnsw (embedding vector_cosine_ops);
```

### 三个实现细节

**① 向量用文本字面量**

```python
"[0.100000,0.200000,...]"   # 显式转 ::vector
```

不用 psycopg 的 list 适配器（会先变 `float[]` 再隐式转换），
报错信息更难懂。

**② 表名白名单**

表名要拼进 SQL，必须 `^[a-z_][a-z0-9_]*$` 校验。参数化救不了标识符。

**③ 全量重建用 TRUNCATE**

`DELETE` 会留死元组、需要 VACUUM；`TRUNCATE` 直接重置，语义也更清楚。

### 什么时候不该上 pgvector

- 单机、向量 < 10 万：numpy 更快（没有网络往返）
- 没有 Postgres 运维能力：多一个服务就多一个故障源
- 只是想要"向量库"这个关键词：面试官会问"为什么"，答不出反而减分

**判断标准**：你需要"多实例共享"或"和业务表同库事务"吗？
不需要，就留在 numpy / sqlite-vec。

---

## 3. 面试怎么讲

**问：为什么项目里既有手写循环又有 LangChain？**

> 同一套工具，五个引擎可切换。默认手写，因为出错时能打断点看到第几步；
> LangChain 版本用来验证"框架能不能做同样的事"，也方便对接生态。
> 实测 LangChain 6 次模型调用、19k token，和手写 14 步、23k 在同一量级。
> 结论不是"谁更好"，是"控制流可以换，工具和契约不能换"。

**问：pgvector 和 sqlite-vec 怎么选？**

> 单机、要增量更新 → sqlite-vec（单文件，不引入服务）。
> 多副本、要和业务表同库事务 → pgvector。
> 10 万条以下其实 numpy 就够，查询几十毫秒。
> 我测过：10 万条 numpy 75ms、sqlite-vec 41ms，只快 1.8 倍，
> 但构建慢 160 倍。所以默认不是向量库，是 numpy。

**问：用了 LangChain 还算自己写的 Agent 吗？**

> 工具、校验器、控制流的不变量都是自己写的。
> LangChain 只替换了"循环"这一层。
> 判断标准是：把框架换掉，业务还能不能跑？
> 这个项目可以——五个引擎共用同一套工具和同一个校验器。

---

## 4. 代价（诚实）

| 加的东西 | 代价 |
|---|---|
| LangChain | 多 7 个依赖；框架升级会破坏 API（AgentExecutor 已被移除） |
| pgvector | 多一个服务；建表/迁移/备份都要管 |
| 五个引擎 | 测试面变大；新人要读五个文件才能理解全貌 |

**都不是"免费的加分项"。** 加之前先能回答"什么时候不用它"。

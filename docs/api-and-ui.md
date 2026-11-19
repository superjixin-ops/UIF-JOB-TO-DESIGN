# 前端与异步：一次请求的完整路径

> 这份文档给你两个东西：**FastAPI 的骨架怎么搭**、**async 用在哪、为什么**。
> 全部对着 `pdc/api.py` 和 `pdc/ui/index.html` 讲，不是通用教程。

---

## 1. 两种任务，同一条流水线

```
POST /runs   {pipeline, mode}     -> 设计文档（YAML → xlsx）
POST /build  {design, max_rounds} -> YAML（xlsx → yaml）
                │
                ▼
        202 {run_id, status: queued}
                │
                ▼
     后台线程跑 Agent（几十秒）
                │
                ▼
   GET /runs/{id}          轮询状态
   GET /runs/{id}/stream   SSE 实时步骤
   GET /files/{name}       下载产物
```

两个入口共用状态机、SSE、文件下载和权限体系。
加第三种任务时，只加一个 `POST` 和一个执行函数，其余复用。

---

## 2. 为什么是 202 + 轮询，不是同步等待

一个 Agent 任务要 20~60 秒。同步等待会：

| 问题 | 后果 |
|---|---|
| HTTP 超时 | 网关/浏览器 30 秒断开，任务还在跑 |
| 连接占满 | 4 个并发就把服务器挂住 |
| 无法取消 | 用户关页面，钱照烧 |

**202 Accepted = "我收下了，去别处看结果"。** 这是异步任务的标准语义。

前端拿到 `run_id` 后：

```js
const data = await fetch('/build', {...}).then(r => r.json());
watch(data.run_id);          // 立刻订阅 SSE
```

---

## 3. 为什么用线程，不是 `async def`

这是最容易搞混的一点。

```python
asyncio.create_task(asyncio.to_thread(_build_and_release, run_id, req, design))
```

**Agent 循环是阻塞的**：`llm.chat()`、`subprocess`、`sqlite3` 都是同步调用。
直接 `await` 会卡住事件循环，所有请求一起卡。

`asyncio.to_thread` 把它丢到线程池：

```
事件循环（单线程）          线程池
  接收请求  ──────────────► 跑 Agent
  继续收请求                  调模型（阻塞）
  推送 SSE                    写 SQLite
```

**判断标准**：

| 你的函数 | 用什么 |
|---|---|
| 纯 IO、有 async 客户端（httpx） | `async def` |
| 阻塞调用（同步 SDK、文件、CPU） | `to_thread` |
| 需要并行多个阻塞任务 | 线程池 / 进程池 |

我们用同步的 OpenAI SDK，所以选 `to_thread`。

---

## 4. SSE 怎么实现的

浏览器端：

```js
const es = new EventSource(`/runs/${id}/stream?token=...`);
es.onmessage = (ev) => { const step = JSON.parse(ev.data); ... };
```

服务端（`stream_run`）：

```python
while True:
    steps = _LIVE.get(run_id, [])      # 内存里的步骤缓冲
    while sent < len(steps):
        yield f"data: {json.dumps(steps[sent])}\n\n"
        sent += 1
    if 任务已结束 and sent >= len(steps):
        yield 'data: {"kind":"done",...}\n\n'
        return
    await asyncio.sleep(0.4)           # 轮询内存，不查库
```

**为什么不用 WebSocket**：只需要服务端单向推送，SSE 是 HTTP，
不用握手、不用心跳、断线浏览器自动重连。

**为什么不在每次工具调用时 push**：那要在 Agent 线程里拿到事件循环的引用，
耦合太紧。用 0.4 秒轮询内存，延迟可接受，代码简单一个数量级。

---

## 5. 一个必须知道的折中：EventSource 不能带 header

`new EventSource(url)` 没有 headers 参数，所以 token 只能走 query：

```
/runs/xxx/stream?token=***
```

代价：token 会进 nginx access log。

生产做法是**短期签名 URL**（一个只能看这个 run 的一次性 token）。
这里写成 query 并在文档里标注，是"知道代价的折中"，不是没想到。

---

## 6. 权限怎么套在端点上

```python
_VIEWER   = require_role("viewer")
_OPERATOR = require_role("operator")
_ADMIN    = require_role("admin")

@app.post("/build")
async def create_build(..., principal: Principal = Depends(_OPERATOR)):
```

三件事同时发生：

1. 没 token → 401（中间件记 `auth_failed` 审计）
2. 角色不够 → 403（记 `authz_denied`）
3. 通过 → `principal` 带进函数，写审计时知道"是谁"

**依赖在模块级建好**（`_OPERATOR = require_role(...)`），
不要在参数默认值里每次调用 `require_role("operator")`——
每次请求都会重建闭包，而且 ruff 会报 B008。

---

## 7. 状态机与持久化

```
queued ──► running ──► succeeded / failed / cancelled
   │
   └─ 进程重启 → mark_orphans() 标成 failed
```

`STORE.update(...)` 是唯一的写入口，字段白名单在 `jobs.py` 里。
**不允许随便加字段**——DB 列是契约，改它要迁移。

`kind` 列就是一次真实迁移：

```python
if "kind" not in cols:
    conn.execute("ALTER TABLE runs ADD COLUMN kind TEXT NOT NULL DEFAULT 'design'")
```

`ADD COLUMN` 是 O(1)，不用重建表、不用停机。

---

## 8. 前端为什么是单文件、没有构建

```html
<script>
  const $ = (id) => document.getElementById(id);
  // 150 行，没有框架、没有 npm、没有打包
</script>
```

理由：

| 方案 | 成本 | 收益 |
|---|---|---|
| React + Vite | 构建链、依赖、CI 多一步 | 组件化 |
| 单文件 | 0 | 改一行刷新就见效 |

这个页面只有两个表单和一条时间线。**上框架是负收益。**

（面试如果问"为什么不用 React"：因为需求只有 150 行，引入构建链会让
部署从"一个文件"变成"一个 dist 目录 + 构建步骤"。）

---

## 9. 怎么加第三种任务（五步）

1. `pdc/api.py` 加 `class XxxRequest(BaseModel)`
2. 加 `_execute_xxx(run_id, req)`，里面调你的 Agent 函数
3. 加 `_xxx_and_release`，`finally: _release()`
4. 加 `@app.post("/xxx")`：校验参数 → `_admit()` → `STORE.create(..., kind="xxx")`
   → 审计 → `asyncio.create_task(asyncio.to_thread(...))` → 202
5. 前端 `kind` 下拉加一项，提交时换 URL

SSE、轮询、下载、权限、指标全部自动复用。

---

## 10. 学习顺序（读 `pdc/api.py`）

| 段 | 行数 | 学什么 |
|---|---|---|
| 1 | 1~60 | FastAPI 实例、中间件、依赖在模块级建好 |
| 2 | `@app.middleware` | 怎么统一记录指标和审计 |
| 3 | `/health`、`/pipelines` | 最简单的 GET + 依赖注入 |
| 4 | `create_run` | 202、并发准入、审计、`to_thread` |
| 5 | `stream_run` | SSE、异步生成器、断线检测 |
| 6 | `_execute_build` | 后台任务里怎么保证状态一定被更新（try/finally） |

每天读一段，对着 `pdc/ui/index.html` 看它怎么被调用。

---

## 11. 四个会真实踩到的坑

**① 后台线程里的异常会被吞**

```python
try:
    ...
except Exception as exc:
    STORE.update(run_id, status="failed", error=...)
finally:
    _CANCELS.discard(run_id)
```

不写 `except`，任务会永远停在 `running`，用户看到转圈。

**② `TestClient` 和真实 uvicorn 不一样**

`TestClient` 里 `asyncio.create_task` 的行为和真实事件循环有差异。
**涉及后台任务的测试，要轮询状态，不要假设"返回 202 就完成了"。**

**③ SSE 会重连**

浏览器断线后自动重连，`EventSource` 会重新发请求。
服务端每次从头回放（`sent=0`），所以页面要能容忍重复步骤。
现在前端用 `s-steps` 计数，重复会多加——这是已知小问题。

**④ 取消不是立即的**

`cancel` 只是打标记，Agent 在**下一次循环开始前**才检查。
正在跑的模型调用不会被打断（那是 SDK 的事）。
返回文案写的是"当前步骤结束后停止"，不是"已停止"。

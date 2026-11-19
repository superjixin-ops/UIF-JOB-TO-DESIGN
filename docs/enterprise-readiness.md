# 企业级能力：从需求到实现

> 这份文档回答三个问题：**为什么做、怎么做、怎么证明它有用。**
> 每一节都按同一个结构写：事故场景 → 方案选择 → 实现 → 验证 → 代价 → 面试话术 → 学习路径。

---

## 0. 先定义：什么叫"企业级"

不是功能多，是**出问题时有人能回答**：

| 问题 | 需要的能力 |
|---|---|
| 这是谁操作的？ | 认证 + 授权 + 审计日志 |
| 服务现在怎么样？ | 指标 + 告警 |
| 数据丢了怎么办？ | 备份 + 恢复演练 |
| 配置写错了怎么办？ | 启动校验 + 快速失败 |
| 出问题怎么定位？ | trace（已有） |

**多租户、水平扩展、K8s** 不在这张表里——它们解决的是"规模"，不是"可控"。
一个没有用户的项目做多租户，做出来只是样子，面试官一问"怎么验证的"就露。

### 当前状态

| 维度 | 能力 | 状态 |
|---|---|---|
| 认证 | Bearer token | ✅ 已有 |
| 授权 | viewer / operator / admin | ✅ 本次 |
| 审计 | 哈希链，防篡改 | ✅ 本次 |
| 指标 | Prometheus 文本格式 | ✅ 本次 |
| 告警 | 5 条规则 + 检查脚本 | ✅ 本次 |
| 备份 | 在线备份 + 校验 + 恢复 | ✅ 本次 |
| 配置 | 启动即校验 | ✅ 本次 |
| 多租户 / HA | —— | ❌ 有意不做 |

---

## 1. RBAC：为什么不是"一个 token 走天下"

### 事故场景

只有一个 token 时，所有拿到它的人权限相同：

```
看板的人 -> 能触发跑批 -> 一次跑批几毛钱，还能写外部系统
实习生   -> 能看审计日志 -> 能看到所有人的操作
```

出事后你无法回答"是谁触发的"，因为大家用的是同一个凭证。

### 方案选择

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| 一个 token | 简单 | 无法区分身份，无法最小权限 | ❌ |
| 每个接口列权限表 | 精细 | 接口一变就漏配 | ❌ |
| **角色 + 最低角色声明** | 接口只声明"最低要什么"，加接口不会漏 | 角色粒度粗 | ✅ |
| OAuth2 / OIDC | 标准、可对接企业 SSO | 需要 IdP，个人项目过重 | 以后 |

**选了第三种**：`viewer < operator < admin`，接口只写 `require_role("operator")`。

### 实现

```python
# pdc/auth.py
_VIEWER   = require_role("viewer")
_OPERATOR = require_role("operator")
_ADMIN    = require_role("admin")
_SSE      = require_role("viewer", allow_query=True)
```

四个细节，每个都有原因：

| 细节 | 为什么 |
|---|---|
| `secrets.compare_digest` | 普通 `!=` 在第一个不同字符处返回，能被时序攻击逐位猜出 token |
| 401 vs 403 | 401 = 你没登录；403 = 登录了但权限不够。混用会让排查方向错 |
| token 只存哈希前缀 | 审计日志里不能出现明文凭证 |
| 默认拒绝 | 没配 token 时，外部访问一律 401；只有本机放行 |

SSE 是例外：浏览器 `EventSource` 不能自定义请求头，只能 `?token=`。
代价是 token 会进 access log——文档里写明了，生产应改短期签名 URL。

### 验证

```bash
pdc doctor
PDC_TOKENS="viewer-token-0001:viewer,operator-token-01:operator,admin-token-00001:admin" \
  uvicorn pdc.api:app --port 8033
```

| 请求 | 期望 |
|---|---|
| `GET /health` 无 token | 200（存活探针不需要鉴权） |
| `GET /metrics` viewer | **403** |
| `GET /metrics` admin | 200 |
| `POST /runs` viewer | **403** |
| `POST /runs` operator | 202 |

测试：`tests/test_enterprise.py::test_rbac_matrix`

### 代价与局限

- 角色只有三级，没有"某个 pipeline 的权限"这种资源级授权。
- token 是静态的，没有过期和轮换机制。生产应该接 OIDC 或短期 token。
- SSE 的 query token 是折中，不是最佳实践。

### 面试话术

> "我把权限做成三级角色 + 接口声明最低角色。接口会变、角色不会，
> 所以加接口时不会漏配。401 和 403 严格分开——401 是没登录，403 是权限不够，
> 混在一起排查方向就错了。"

### 学习路径

先读 `pdc/auth.py`（90 行），再看 `pdc/api.py` 里每个端点的 `Depends(_OPERATOR)`。

---

## 2. 审计日志：为什么不是"写个 JSONL"

### 事故场景

```
凌晨 2 点，有人跑了一次全量，把下游表覆盖了。
问：是谁？
```

没有审计日志，你只有 trace（记录了模型和工具），没有"谁"。

### 方案选择

| 方案 | 能回答"谁" | 能防篡改 | 成本 |
|---|---|---|---|
| 应用日志 | 能（如果记得打） | ❌ 谁都能改 | 低 |
| JSONL 文件 | 能 | ❌ | 低 |
| 普通 SQLite 表 | 能 | ❌ DBA 能改 | 低 |
| **哈希链 SQLite** | 能 | ✅ 改一条，后面全对不上 | 低 |

哈希链：`hash = sha256(prev_hash + 内容)`。改第 2 条，第 3 条开始全部失效。

### 实现

```python
# pdc/audit_log.py
prev = 上一条的 hash（第一条用 64 个 0）
payload = {ts, actor, role, action, target, result, ip, detail}
digest = sha256(prev + canonical_json(payload))
```

**只记这些，不记什么**：

| 记 | 不记 |
|---|---|
| token_id（哈希前 12 位） | token 明文 |
| 动作（run.create / run.cancel / auth_failed） | 请求体全文 |
| 目标 pipeline | 模型输出 |
| 结果、IP | 文件内容 |

### 验证

```bash
pdc audit-log verify          # {"ok": true, "first_bad_seq": null}
pdc audit-log list --limit 5
```

测试里直接改库：

```python
conn.execute("UPDATE events SET target = 'tampered' WHERE seq = 2")
assert verify() == (False, 2)
```

### 代价与局限

- 写入是串行的（有锁），高并发下会成为瓶颈。目前单实例、低频操作，够用。
- 没有归档和保留策略，表会一直长。
- 哈希链只能证明"被改过"，不能阻止改。真正的防篡改需要外部存储（S3 Object Lock）。

### 面试话术

> "审计日志用哈希链，每条带前一条的哈希。改任何一条，后面全部对不上——
> 这是最便宜的防篡改方案。里面只存 token 的哈希前缀，不存明文。"

### 学习路径

`pdc/audit_log.py` 的 `append` 和 `verify` 两个函数，20 行核心逻辑。

---

## 3. 指标：为什么不用 prometheus-client

### 事故场景

```
用户说"你们服务很慢"。
你：有多慢？从什么时候开始？多少请求受影响？
```

没有指标，只能靠感觉。

### 方案选择

| 方案 | 依赖 | 控制力 | 结论 |
|---|---|---|---|
| prometheus-client | 一个包 | 中 | 可以用 |
| OpenTelemetry | 一套 SDK | 强 | 过重 |
| **手写文本格式** | 0 | 完全 | ✅ |

只需要 4 种原语（counter / gauge / histogram / 文本），100 行够。

### 关键设计：低基数

```python
# ❌ 每个 run_id 一个标签 -> 无限多时间序列
{"path": f"/runs/{run_id}"}

# ✅ 用路由模板
{"path": "/runs/{run_id}"}
```

这一条不做，Prometheus 会被打爆——这是新手最容易犯的错。

### 指标清单

| 指标 | 类型 | 用途 |
|---|---|---|
| `pdc_http_requests_total{method,path,status}` | counter | 流量、错误率 |
| `pdc_http_request_duration_seconds` | histogram | p95 延迟 |
| `pdc_auth_failures_total` | counter | 扫描 / token 轮换问题 |
| `pdc_authz_denied_total` | counter | 权限配置错误 |
| `pdc_rate_limited_total` | counter | 限流是否过紧 |
| `pdc_runs_total{status}` | counter | 业务量 |
| `pdc_queue_depth` | gauge | 积压 |

### 实现细节

直方图内部存"落在本桶"的计数，输出时要转成**累计值**：

```
_bucket{le="0.05"} 3     # <= 0.05 的有 3 个
_bucket{le="0.1"}  5     # <= 0.1 的有 5 个（含前面 3 个）
```

直接输出非累计值，分位数会算错，而且错得很隐蔽——测试里专门锁了这一条。

### 验证

```bash
curl -H "Authorization: Bearer $ADMIN" localhost:8000/metrics
```

测试：`test_metrics_render_has_types_and_labels`、`test_percentile_from_hist`

---

## 4. 告警：阈值在配置，判定在代码

### 事故场景

指标有了，但没人看。**没有告警的指标等于没有。**

### 方案选择

| 方案 | 优点 | 缺点 |
|---|---|---|
| 写 PromQL | 标准 | 需要 Prometheus；规则散落 |
| 全部硬编码 | 简单 | 改阈值要改代码 |
| **规则类型在代码，阈值在 YAML** | 边界清楚 | 加新类型要改代码 |

### 五条规则

```yaml
high_error_rate      5xx 占比 > 5%         page
slow_p95             p95 > 3s              ticket
auth_failure_spike   认证失败 >= 20        page
queue_saturated      积压 > 8              ticket
no_traffic           总请求 < 1            info
```

**退出码分级**：0 无告警 / 1 ticket / 2 page。
这样可以挂 cron、k8s probe、CI，不同级别走不同通知。

### 验证

```bash
python -m scripts.check_alerts --url http://127.0.0.1:8033/metrics --token $ADMIN
# OK 无告警   或   [{...}]  exit=1/2
```

测试里用构造的指标文本跑规则，不需要起服务。

### 代价与局限

- 没有通知通道（邮件/Slack），只有退出码。
- 没有 `for: 5m` 的持续时间窗口——单次采样就触发，会有抖动。
- 分位数是桶上界估算，不是精确值。

---

## 5. 备份：为什么不能 `cp jobs.db`

### 事故场景

```
磁盘满 -> jobs.db 损坏 -> 三个月的任务记录没了
```

### 方案选择

| 方案 | 一致性 | 需要停服 | 能验证 |
|---|---|---|---|
| `cp jobs.db` | ❌ 可能拿到半写状态 | 最好停 | ❌ |
| `sqlite3 .backup` | ✅ 事务边界 | 不用 | ✅ |
| **`Connection.backup()` API** | ✅ | 不用 | ✅ |

### 实现三个动作

```
create   在线备份 -> 每个库算 sha256 + 表行数 -> manifest.json -> tar.gz -> 轮转
verify   解包 -> 重算哈希 -> PRAGMA integrity_check -> 比对行数
restore  先 verify -> 目标非空则拒绝（除非 --force）-> 复制
```

**为什么带行数**：哈希只能证明"文件没变"，行数能发现"备份的是个空库"。

**为什么 restore 要拒绝非空目录**：手滑覆盖生产库是经典事故。

### 踩过的坑：WAL 边车文件

第一版归档里带了 `audit.db-wal` 和 `audit.db-shm`。

原因：`with sqlite3.connect()` **只提交事务，不关闭连接**。
连接没关，WAL 文件还在，被打进了归档。

修法：显式 `close()`，备份后 `wal_checkpoint(TRUNCATE)` + `journal_mode=DELETE`，
再删掉边车文件。恢复时也跳过 `-wal/-shm`。

测试：`test_backup_archive_excludes_wal_sidecars`

### 验证

```bash
pdc backup create --out backups --keep 7
pdc backup verify --archive backups/backup-20261119-165805.tar.gz
pdc backup restore --archive ... --to /tmp/restored-test
```

测试：`test_backup_verify_and_restore`、`test_backup_verify_detects_corruption`、
`test_restore_refuses_non_empty_target`

### 代价与局限

- 只备份 SQLite，不备份 `out/` 里的产物文件和索引。
- 没有定时任务，只有命令；生产要挂 cron 或 k8s CronJob。
- 没有异地副本。

---

## 6. 配置校验：把"运行时炸"变成"启动时拒绝"

### 事故场景

```
PDC_MAX_CONCURRENCY=0  -> 所有请求 429，看起来像限流故障
PDC_PORT=abc           -> 三十行报错之后才看到 "invalid literal for int()"
PDC_OUT_DIR=/root/out  -> 跑了五分钟才发现不可写，钱已经花了
```

这些都能在启动时发现。

### 方案选择

| 方案 | 错误信息 | 依赖 |
|---|---|---|
| 直接 `int(os.getenv(...))` | 差 | 0 |
| pydantic Settings | 中（"value is not a valid integer"） | 1 |
| **手写 60 行** | 好（"应该写成 8000 这样的数字"） | 0 |

### 实现

`Settings.load()` 收集**全部**问题，一次性抛出：

```
配置有问题：
  - PDC_PORT='abc' 不是整数，应该写成 8000 这样的数字
  - PDC_MAX_CONCURRENCY=0 超出范围，应该在 1~64 之间
  - PDC_RAG_MODE='magic' 不认识，只能是 bm25 或 hybrid
```

启动时 `Settings.load().validate()`，有问题直接起不来。
`pdc doctor` 可以在不启动服务的情况下检查。

### 验证

```bash
pdc doctor
PDC_MAX_CONCURRENCY=0 pdc doctor   # 退出码 1 + 具体问题
```

测试：`test_config_reports_all_problems_at_once`、token 解析三条。

---

## 7. 为什么不做多租户和水平扩展

| 能力 | 为什么不做 |
|---|---|
| 多租户 | 没有第二个用户。做了也无法验证隔离是否有效 |
| 水平扩展 | 单实例没有负载。做了也测不出收益，只会增加"看起来复杂" |
| K8s | 这个岗位不考，而且没有集群 |
| 异地容灾 | 没有真实数据需要保 |

> **企业级不是"把大厂架构图抄一遍"，是"每个能力都能回答：为什么需要、怎么验证"。**
> 答不出来的，不做比做了好。

---

## 8. 怎么演示（5 分钟）

```bash
# 1. 配置检查
pdc doctor

# 2. 起服务（三个角色）
export PDC_TOKENS="viewer-token-0001:viewer,operator-token-01:operator,admin-token-00001:admin"
uvicorn pdc.api:app --port 8033

# 3. 权限矩阵
curl -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer viewer-token-0001" \
  -X POST localhost:8033/runs -d '{}'          # 403

# 4. 审计 + 防篡改
curl -H "Authorization: Bearer admin-token-00001" localhost:8033/audit/verify

# 5. 指标 + 告警
python -m scripts.check_alerts --url localhost:8033/metrics --token admin-token-00001

# 6. 备份 + 恢复
pdc backup create --out backups
pdc backup verify --archive backups/backup-*.tar.gz
```

---

## 9. 学习顺序（零基础）

| 顺序 | 文件 | 行数 | 学什么 |
|---|---|---|---|
| 1 | `pdc/config.py` | ~150 | 环境变量、数据类、错误聚合 |
| 2 | `pdc/auth.py` | ~100 | 依赖注入、哈希、时序攻击 |
| 3 | `pdc/audit_log.py` | ~150 | SQLite、哈希链、线程锁 |
| 4 | `pdc/metrics.py` | ~150 | 计数器/直方图、文本格式 |
| 5 | `pdc/backup.py` | ~180 | 文件操作、tar、完整性校验 |
| 6 | `tests/test_enterprise.py` | ~200 | 怎么"故意弄坏"来验证 |

每天一个，一周读完。**每个文件先看测试，再看实现**——测试会告诉你它要保证什么。

# 检索的切分与测试集设计

> 这份文档回答两个问题：**chunk 怎么切**、**测试集怎么造**。
> 两个问题的共同点：做错了，后面的所有指标都会骗你。

---

## 一、切分：按语义边界，不按行数

### 1.1 三种用途，三种切法

不要用一个 chunk 服务所有场景。这个项目里有三种检索：

| 用途 | 切分单位 | 大小 | 必须带的信息 |
|---|---|---|---|
| **few-shot 示例** | 整条 pipeline（或整个 job） | 100~300 行 | pipeline 名、job 链、规模 |
| **结构检索** | 一张结构卡 | 1 行 | job 链、连接器、变换类型 |
| **文档 RAG** | 一个 `##` 小节 | 200~500 token | 文件、标题路径 |

给 few-shot 一篇 600 行的 YAML 是浪费；给文档 RAG 一整条 pipeline 是噪音。

### 1.2 pipeline YAML 怎么切

**按 job 切，不按行切。**

一条 600 行的 pipeline 通常有 3~4 个 job，每个 job 是一个完整单元：

```
pipeline/fact-order-fulfillment
├── meta                    apiVersion / kind / metadata / owner
├── job[0]                  eid（extract + transform + load）
├── job[1]                  datamart-parquet
└── job[2]                  datamart-self-serve
```

单个 job 太大时（>300 行），再按三段切：

| chunk | 内容 | 为什么单独切 |
|---|---|---|
| `job[0]/extract` | 数据源、连接器、凭证、query | 回答"数据从哪来" |
| `job[0]/transform` | 变换序列、参数 | 回答"怎么处理" |
| `job[0]/load` | 目标表、schema、分区 | 回答"写到哪、什么契约" |

**但每个 chunk 必须带血缘锚点**：

```yaml
# chunk: pipeline=fx, job=eid, section=transform
jobType: eid
objectNames: [ORDER_LINES]      # 上游
aliasResult: ORDER_LINES        # 下游
```

缺了 `objectNames` / `aliasResult`，这个 chunk 就孤立了——模型看到一串变换，
不知道数据从哪来、到哪去。

### 1.3 六条硬规则

| # | 规则 | 反面例子 |
|---|---|---|
| 1 | 按语义边界切（job / `##` 标题 / 表格） | 按 50 行切，把一个 transform 列表劈成两半 |
| 2 | 每个 chunk 自包含 | 只存 `- transformType: Drop`，不存它属于哪个 job |
| 3 | 带完整路径 | `chunk_id = pipeline/job/section` |
| 4 | 不跨块 | 一个 YAML block 不能被切开 |
| 5 | 代码块 / 表格整体保留 | 把 markdown 表格从中间切开 |
| 6 | 重叠只在长文里用（10~15%） | YAML 不需要重叠，重叠只会造成重复召回 |

### 1.4 一个具体的切分示例

原始（截断）：

```yaml
- jobType: eid
  extract:
    - alias: ORDER_LINES
      main_df: true
      connections:
        - credentials: demo-cos
          connector: cos
          query: landing/orders/order_lines/transform.parquet
  transform:
    - transformType: RenameColumns
      objectNames: [ORDER_LINES]
      mapColumns: {ORDER_LINE_ID: ORDER_LINE_NUMBER}
      aliasResult: ORDER_LINES
  load:
    metadata: {name: FACT_ORDER_FULFILLMENT}
    connections:
      - mode: overwrite
        loadObject: FACT_ORDER_FULFILLMENT
        partitionBy: [ORDER_DATE]
    schema:
      - {columnName: ORDER_FULFILLMENT_KEY, dataType: IntegerType, nullAble: false}
```

切成 3 块，每块都带 `jobType` 和锚点：

```yaml
# chunk_id: fact-order-fulfillment/eid/extract
jobType: eid
extract:
  - alias: ORDER_LINES
    main_df: true
    connections: [{credentials: demo-cos, connector: cos,
                   query: landing/orders/order_lines/transform.parquet}]
```

```yaml
# chunk_id: fact-order-fulfillment/eid/transform
jobType: eid
inputs: [ORDER_LINES]
output: ORDER_LINES
transform:
  - transformType: RenameColumns
    mapColumns: {ORDER_LINE_ID: ORDER_LINE_NUMBER}
```

```yaml
# chunk_id: fact-order-fulfillment/eid/load
jobType: eid
inputs: [ORDER_LINES]
load:
  metadata: {name: FACT_ORDER_FULFILLMENT}
  connections: [{mode: overwrite, loadObject: FACT_ORDER_FULFILLMENT,
                 partitionBy: [ORDER_DATE]}]
  schema: [...]
```

### 1.5 切分质量怎么验（不花钱）

切完先跑四个硬门禁，任何一个不过就不许进检索评测：

| 检查 | 方法 | 通过线 |
|---|---|---|
| **结构完整** | YAML chunk 能否独立解析 | 100% |
| **路径完整** | 每个 chunk 有 `pipeline/job/section` | 100% |
| **答案覆盖** | 测试集里每个 gold 答案完整落在某个 chunk 内 | 100% |
| **重叠率** | 相邻 chunk 的 token 重叠 | < 20% |

第三条最容易被忽略：如果标准答案跨了两个 chunk，召回永远算不对，
而你可能会误以为是"检索效果差"。

---

## 二、测试集：怎么造，怎么防自欺

### 2.1 四层测试，各测各的

```
第一层  切分质量      无模型、无标注     结构 / 路径 / 覆盖 / 重叠
第二层  检索质量      要标注、不要模型   R@1/3/5、MRR、负样本
第三层  生成质量      要模型             pass_rate、轮数、token
第四层  端到端        要人               产物能不能被评审接受
```

**顺序不能反**：切分错了，第二层的数字没有意义。

### 2.2 三条造题原则

**原则一：不许从答案反推**

这是本项目真实踩过的坑：

```
拿 pipeline A 生成设计文档 -> 文档里全是 A 的表名和列名
-> 拿它当 query 去检索 -> 必然命中 A
-> 命中率 100%，但证明不了任何事
```

**正确做法**：query 必须来自"需求侧"——人写的需求、工单、或者改写过的描述。

**原则二：留一法**

候选集里不能有 gold。生成 A 时，A 必须从索引里排除。

**原则三：分层**

| 层 | 特征 | 测什么 |
|---|---|---|
| easy | 含表名/系统名 | 词面匹配的上限 |
| hard | 只有业务描述 | 理解意图 |
| negative | 语料里根本没有 | 该空的时候会不会硬塞 |

easy 和 hard 的**差值**就是"词面泄漏贡献了多少命中率"。
negative 测的是"不乱答"——大部分评测都不测这个，但它最接近真实体验。

### 2.3 测试集规模与成本

| 集合 | 条数 | 来源 | 耗时 |
|---|---|---|---|
| 文档 RAG | 50 | 每个 `##` 小节写 1~2 个问题 | 2 小时 |
| 结构检索 | 50 | 从卡片自动生成 query，人工确认 | 1 小时 |
| 语义检索 | 20 | 人工写（需要业务知识） | 1 小时 |
| negative | 10 | 故意问语料里没有的 | 20 分钟 |

**总计约 4 小时。** 这是整个检索项目里最贵的一步，也是最值钱的一步。

### 2.4 标注纪律

| 规则 | 原因 |
|---|---|
| 先读完所有候选，再写 query | 避免边看检索结果边改题 |
| 写完不再修改 | 改一次就是拿答案调参 |
| 隔天复核一遍 | 自己当天看不出自己的偏差 |
| 记录标注耗时 | 面试会问"这 50 条花了多久" |

**可选的加分项**：两个人各标 20 条，算一致率（Cohen's kappa）。
一致率低于 0.6 说明"什么算相关"没定义清楚，先对齐再标。

### 2.5 指标怎么读

| 指标 | 用途 | 陷阱 |
|---|---|---|
| R@5 | 有没有进候选 | 34 个 chunk 时 R@5 天然很高 |
| R@1 | 排得对不对 | 受标注歧义影响最大 |
| MRR | 平均名次 | 一条差样本能拉低很多 |
| **负样本准确率** | 该空的时候空 | 很少人报，但最影响体验 |

**必须同时报基线**：

- 随机（下界）
- 文件名关键词匹配（强基线）
- 无检索（对照组）

只报"我的检索 R@5 = 0.8"没有意义——文件名匹配可能也是 0.8。

### 2.6 一个已经踩过的坑

写 34 条样例的检索评测时，我为了"公平"，写 query 时刻意不抄原文关键词。

结果 BM25 的 R@5 只有 0.214，看起来"词法检索完全不行"。

**但这个结论是标注方式造成的**：query 全是被改写过的话，词法当然吃亏。

> **标注方式本身会决定谁赢。**
> 报指标时，必须同时报"query 是怎么写的"。

---

## 三、这件事和"要不要向量数据库"的关系

| 问题 | 切分/测试集能回答吗 |
|---|---|
| 要不要上向量库 | ❌ 不能。它只影响检索质量和规模 |
| 检索有没有用 | ✅ 能（第二层） |
| 检索有用，值不值得接进 Agent | ✅ 能（第三层） |
| 换了向量库会不会更好 | ✅ 能（同一套测试集跑两个后端） |

**顺序永远是**：

```
切分合格 -> 检索有效 -> 生成有收益 -> 才谈换基础设施
```

反过来做（先上库，再想怎么测），最后一定得到一个"跑得很快但没人用"的系统。

---

## 四、本项目当前进度

| 步骤 | 状态 |
|---|---|
| 语料体检（5,816 条） | ✅ 完成 |
| 结构卡片 + 三级查表 | ✅ 完成（覆盖 82%） |
| few-shot 下游 A/B | 🔄 进行中（12 条 × 3 组） |
| 语义检索测试集 | ⬜ 待做（需要 20 条人工 query） |
| 切分实现 + 四个门禁 | ⬜ 待做 |
| 向量库后端对比 | ⬜ 待做 |

上表的空白项不是"没想清楚"，是**故意的顺序**：
先把不花钱的验证做完，再决定要不要引入新组件。

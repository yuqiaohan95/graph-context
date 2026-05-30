# Graph Context

**MCP 代码上下文引擎 — 87%+ Token 节省，零资源消耗。**

基于 AST 调用图 + BM25 精排的 MCP 上下文引擎，为 AI 编码助手提供精准的代码上下文检索。不需要 GPU，不需要外部服务，纯 Python 实现，开箱即用。

[English](README.md)

## 核心优势

| 指标 | 无 MCP（全量上下文） | 有 MCP（Graph Context） |
|------|---------------------|------------------------|
| 单轮 Token 消耗 | 全部代码上下文 | 仅相关 chunks（精准检索） |
| 10 轮对话累计 | 持续膨胀，触发遗忘 | 稳定在 ~2000 tokens/轮 |
| 多 Agent 协同 | 每个 Agent 重复加载上下文 | MVCC 快照，共享索引，读写隔离 |
| **Token 节省率** | — | **87%+（单 Agent & 多 Agent）** |
| 资源消耗 | — | **零**（纯 CPU，无模型调用） |

## 工作原理

```
用户查询 → 中英文同义词扩展 → BM25 精排（AST 调用图加权）
                                    ↓
                              IDF 噪声过滤 → 粗细筛选（类→方法下钻）
                                    ↓
                              规则 Boost（自进化规则引擎）
                                    ↓
                              返回 top-k 精准 chunks
```

**三层检索，逐层精炼：**

1. **AST 调用图** — 函数→函数精确映射，不是 token 共现。跨文件依赖一跳直达。
2. **BM25 精排** — 信息检索标准评分，结合 chunk 类型权重（function > class > imports）。
3. **规则自进化** — 新规则先进入观察期，基于准确率衰减（不是时间），低效规则自动淘汰。

## 快速开始

### 安装

```bash
pip install graph-context

# 或带完整功能（中文分词 + 文件监听）
pip install graph-context[full]
```

### 作为 MCP Server 运行

```bash
# 基本运行
graph-context

# 指定项目目录
PROJECT_ROOT=/path/to/your/project graph-context

# 自定义配置
PROJECT_ROOT=/path/to/project MCP_MAX_TOKENS=4000 MCP_TOP_K=10 graph-context
```

### 在 Claude Desktop / Cursor / Cline 中配置

```json
{
  "mcpServers": {
    "graph-context": {
      "command": "graph-context",
      "env": {
        "PROJECT_ROOT": "/path/to/your/project"
      }
    }
  }
}
```

### 在代码中使用

```python
from graph_context import VibeCodingEngine, DEFAULT_CONFIG

engine = VibeCodingEngine("/path/to/project", DEFAULT_CONFIG)
engine.index_project()

# 检索相关代码
results = engine.retrieve("用户认证是怎么实现的", top_k=5)
for score, chunk in results:
    print(f"[{score:.2f}] {chunk.file_path}:{chunk.name}")
```

## MCP 工具一览

### 检索工具

| 工具 | 说明 |
|------|------|
| `retrieve_context` | 基础检索：BM25 + AST 调用图 |
| `retrieve_context_adaptive` | 自适应策略：小项目全量评分，大项目图扩散 |
| `retrieve_with_dependencies` | 检索 + 跨文件依赖（函数→调用方一跳直达） |
| `batch_retrieve` | 批量检索，一次查多个问题 |
| `compare_strategies` | 对比不同策略的 token 消耗 |

### MVCC 快照（多 Agent 协同）

| 工具 | 说明 |
|------|------|
| `create_snapshot` | 创建 MVCC 快照（COW，无 deepcopy） |
| `read_at_version` | 读取指定版本的索引状态 |
| `get_mvcc_status` | 查看当前版本和快照列表 |

**多 Agent 场景：** 每个 Agent 创建独立快照，读写互不干扰。Agent A 写入新索引不影响 Agent B 正在读的版本。共享底层索引，零冗余。

### 规则引擎（自进化）

| 工具 | 说明 |
|------|------|
| `add_rule` | 添加规则（自动进入观察期） |
| `list_rules` | 列出规则（支持按类型/作用域/置信度过滤） |
| `evaluate_rules` | 用 Ground Truth 评估规则效果 |
| `apply_rule` | 应用规则到检索流程 |
| `verify_rule` | 验证规则（正确/错误），驱动准确率衰减 |
| `prune_rules` | 淘汰低效规则 |
| `discover_rules` | 从 Ground Truth 错误中自动发现候选规则 |
| `check_rule_conflicts` | 检测规则冲突 |
| `check_code_freshness` | 检查规则关联的代码是否已变更 |

### 项目作用域（多项目管理）

| 工具 | 说明 |
|------|------|
| `create_scope` | 创建项目作用域（strict 隔离 / shared 打通） |
| `link_projects` | 关联项目（单向/双向/完全共享） |
| `search_across_projects` | 跨项目搜索模式（只返回模式，不返回代码） |
| `list_scopes` | 列出所有项目作用域 |

### 系统工具

| 工具 | 说明 |
|------|------|
| `health_check` | 引擎健康检查（索引状态、文件监听、缓存、MVCC） |
| `get_config` | 获取当前配置 |
| `update_config` | 运行时更新配置 |

## 配置

通过环境变量配置，所有配置项都有合理默认值：

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `PROJECT_ROOT` | 当前目录 | 项目根目录 |
| `MCP_MAX_TOKENS` | 3000 | 最大上下文 token 数 |
| `MCP_TOP_K` | 8 | 默认返回结果数 |
| `MCP_MAX_HOPS` | 2 | 调用图最大跳数 |
| `MCP_PERSIST_PATH` | 无 | 索引持久化路径 |
| `MCP_CACHE_SIZE` | 128 | 查询缓存大小 |
| `MCP_RULES_PATH` | `rules/rules.json` | 规则存储路径 |
| `MCP_SCOPES_PATH` | `scopes/scopes.json` | 作用域存储路径 |

## Token 节省原理

### 单 Agent 场景

传统方式：每轮对话把整个项目上下文塞进 prompt → token 线性膨胀 → 触发遗忘 → 用户追问 → 更多 token。

Graph Context：每轮只检索相关 chunks（~2000 tokens）→ 上下文稳定 → 不遗忘 → 不追问。

```
传统方式（10 轮对话）:
  轮 1:  800 + 2000 = 2,800 tokens
  轮 5:  800 + 12000 = 12,800 tokens  ← 开始遗忘
  轮 10: 800 + 25000 = 25,800 tokens  ← 频繁追问
  累计: ~150,000 tokens

Graph Context（10 轮对话）:
  轮 1:  800 + 2000 = 2,800 tokens
  轮 5:  800 + 2000 = 2,800 tokens   ← 稳定
  轮 10: 800 + 2000 = 2,800 tokens   ← 稳定
  累计: ~28,000 tokens
  节省: 87%+
```

### 多 Agent 协同场景

多个 Agent 共享同一个 Graph Context 实例，通过 MVCC 快照实现读写隔离：

- **共享索引**：所有 Agent 共用一份代码索引，不重复构建
- **读写隔离**：每个 Agent 创建独立快照，写入不影响其他 Agent 的读取
- **零冗余**：COW（Copy-on-Write）机制，快照只存储差异

```
传统方式（3 个 Agent 各自加载上下文）:
  Agent A: 15,000 tokens
  Agent B: 15,000 tokens  ← 重复加载
  Agent C: 15,000 tokens  ← 重复加载
  累计: 45,000 tokens

Graph Context（3 个 Agent 共享索引）:
  共享索引: 1 次构建
  Agent A 快照: ~2,000 tokens（只读相关 chunks）
  Agent B 快照: ~2,000 tokens
  Agent C 快照: ~2,000 tokens
  累计: ~6,000 tokens
  节省: 87%+
```

## 为什么不需要额外资源

| 对比 | 向量数据库方案 | Graph Context |
|------|--------------|---------------|
| 外部依赖 | 需要部署向量数据库 | 无 |
| GPU | 嵌入模型需要 GPU | 无 |
| 网络调用 | 嵌入 API 调用 | 无 |
| 内存 | 向量索引占用大 | 轻量（BM25 倒排索引） |
| 冷启动 | 需要预计算嵌入 | 秒级 AST 解析 |
| 准确率 | 语义相似但不精确 | AST 级精确匹配 |

## 支持的语言

**代码解析（AST）：** Python, JavaScript, TypeScript, JSX, TSX, Vue, Svelte, Go, Rust, Java, Kotlin, C, C++, Shell

**查询语言：** 中文、英文（内置中英文同义词映射，零成本语义桥接）

## 项目结构

```
graph-context/
├── graph_context/
│   ├── __init__.py          # 包入口
│   ├── __main__.py          # python -m graph_context
│   ├── engine.py            # 核心引擎（AST + BM25 + 图扩散）
│   ├── server.py            # MCP Server（25+ 工具）
│   ├── rules.py             # 规则自进化引擎
│   ├── project_scope.py     # 多项目作用域管理
│   ├── synonyms.py          # 中英文同义词映射
│   ├── ground_truth.py      # Ground Truth 评估
│   └── ground_truth_report.py
├── tests/
│   ├── experiment.py        # 功能集成测试
│   ├── stress_test.py       # 压力测试
│   └── token_simulation.py  # Token 消耗模拟器
├── pyproject.toml
├── LICENSE
└── README.md
```

## 运行测试

```bash
# 功能测试
python -m tests.experiment

# 压力测试
python -m tests.stress_test

# Token 消耗模拟（生成报告）
python -m tests.token_simulation
```

## License

[MIT](LICENSE)

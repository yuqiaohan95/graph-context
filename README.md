# Graph Context

**MCP server for intelligent code context — 87%+ token savings, zero resource overhead.**

An MCP context engine built on AST call graphs + BM25 ranking, delivering precise code context retrieval for AI coding assistants. No GPU, no external services, pure Python, ready to use out of the box.

[中文文档](README_zh.md)

## Why Graph Context

| Metric | Without MCP (full context) | With MCP (Graph Context) |
|--------|---------------------------|-------------------------|
| Per-turn token usage | Entire codebase | Only relevant chunks (precision retrieval) |
| 10-turn conversation | Linear growth, triggers forgetting | Stable at ~2,000 tokens/turn |
| Multi-agent collaboration | Each agent reloads context independently | MVCC snapshots, shared index, read-write isolation |
| **Token savings** | — | **87%+ (single-agent & multi-agent)** |
| Resource consumption | — | **Zero** (pure CPU, no model calls) |

## How It Works

```
User query → Synonym expansion (CN/EN) → BM25 ranking (AST call graph weighted)
                                              ↓
                                        IDF noise filtering → Coarse-to-fine (class → method drill-down)
                                              ↓
                                        Rule boost (self-evolving rules engine)
                                              ↓
                                        Return top-k precise chunks
```

**Three-layer retrieval, progressively refined:**

1. **AST Call Graph** — Function-to-function precise mapping, not token co-occurrence. Cross-file dependencies in one hop.
2. **BM25 Ranking** — Standard information retrieval scoring, combined with chunk type weights (function > class > imports).
3. **Self-evolving Rules** — New rules enter observation period first, decay based on accuracy (not time), low-performing rules auto-pruned.

## Quick Start

### Install

```bash
pip install graph-context

# With full features (Chinese tokenization + file watching)
pip install graph-context[full]
```

### Run as MCP Server

```bash
# Basic
graph-context

# Specify project root
PROJECT_ROOT=/path/to/your/project graph-context

# Custom configuration
PROJECT_ROOT=/path/to/project MCP_MAX_TOKENS=4000 MCP_TOP_K=10 graph-context
```

### Configure in Claude Desktop / Cursor / Cline

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

### Use in Code

```python
from graph_context import VibeCodingEngine, DEFAULT_CONFIG

engine = VibeCodingEngine("/path/to/project", DEFAULT_CONFIG)
engine.index_project()

# Retrieve relevant code
results = engine.retrieve("how does user authentication work", top_k=5)
for score, chunk in results:
    print(f"[{score:.2f}] {chunk.file_path}:{chunk.name}")
```

## MCP Tools

### Retrieval

| Tool | Description |
|------|-------------|
| `retrieve_context` | Basic retrieval: BM25 + AST call graph |
| `retrieve_context_adaptive` | Adaptive strategy: full-context for small projects, graph diffusion for large |
| `retrieve_with_dependencies` | Retrieval + cross-file dependencies (function → caller in one hop) |
| `batch_retrieve` | Batch retrieval, multiple queries at once |
| `compare_strategies` | Compare token usage across different strategies |

### MVCC Snapshots (Multi-Agent Collaboration)

| Tool | Description |
|------|-------------|
| `create_snapshot` | Create MVCC snapshot (COW, no deepcopy) |
| `read_at_version` | Read index state at a specific version |
| `get_mvcc_status` | View current version and snapshot list |

**Multi-agent scenario:** Each agent creates an independent snapshot. Read-write operations don't interfere with each other. Agent A writing new index data doesn't affect Agent B's ongoing read. Shared underlying index, zero redundancy.

### Rules Engine (Self-evolving)

| Tool | Description |
|------|-------------|
| `add_rule` | Add a rule (auto-enters observation period) |
| `list_rules` | List rules (filter by type/scope/confidence) |
| `evaluate_rules` | Evaluate rule effectiveness with ground truth |
| `apply_rule` | Apply a rule to the retrieval pipeline |
| `verify_rule` | Verify a rule (correct/incorrect), drives accuracy-based decay |
| `prune_rules` | Prune low-performing rules |
| `discover_rules` | Auto-discover candidate rules from ground truth errors |
| `check_rule_conflicts` | Detect rule conflicts |
| `check_code_freshness` | Check if rule-associated code has changed |

### Project Scopes (Multi-project Management)

| Tool | Description |
|------|-------------|
| `create_scope` | Create project scope (strict isolation / shared) |
| `link_projects` | Link projects (one-way / bidirectional / full sharing) |
| `search_across_projects` | Cross-project pattern search (returns patterns only, no code) |
| `list_scopes` | List all project scopes |

### System

| Tool | Description |
|------|-------------|
| `health_check` | Engine health check (index stats, file watcher, cache, MVCC) |
| `get_config` | Get current configuration |
| `update_config` | Update configuration at runtime |

## Configuration

Configured via environment variables, all with sensible defaults:

| Env Variable | Default | Description |
|-------------|---------|-------------|
| `PROJECT_ROOT` | Current directory | Project root directory |
| `MCP_MAX_TOKENS` | 3000 | Max context tokens |
| `MCP_TOP_K` | 8 | Default number of results |
| `MCP_MAX_HOPS` | 2 | Max hops in call graph |
| `MCP_PERSIST_PATH` | None | Index persistence path |
| `MCP_CACHE_SIZE` | 128 | Query cache size |
| `MCP_RULES_PATH` | `rules/rules.json` | Rules storage path |
| `MCP_SCOPES_PATH` | `scopes/scopes.json` | Scopes storage path |

## Token Savings Explained

### Single-Agent Scenario

**Without MCP:** Each turn dumps the entire project context into the prompt → token count grows linearly → triggers forgetting → user has to repeat context → more tokens wasted.

**With Graph Context:** Each turn retrieves only relevant chunks (~2,000 tokens) → context stays stable → no forgetting → no repeated explanations.

```
Without MCP (10-turn conversation):
  Turn 1:   800 + 2,000  =  2,800 tokens
  Turn 5:   800 + 12,000 = 12,800 tokens  ← forgetting begins
  Turn 10:  800 + 25,000 = 25,800 tokens  ← frequent follow-ups
  Total: ~150,000 tokens

With Graph Context (10-turn conversation):
  Turn 1:   800 + 2,000 = 2,800 tokens
  Turn 5:   800 + 2,000 = 2,800 tokens   ← stable
  Turn 10:  800 + 2,000 = 2,800 tokens   ← stable
  Total: ~28,000 tokens
  Savings: 87%+
```

### Multi-Agent Collaboration Scenario

Multiple agents share a single Graph Context instance, using MVCC snapshots for read-write isolation:

- **Shared index:** All agents share one code index, built once
- **Read-write isolation:** Each agent creates an independent snapshot; writes don't affect other agents' reads
- **Zero redundancy:** COW (Copy-on-Write) mechanism, snapshots only store deltas

```
Without MCP (3 agents each loading context independently):
  Agent A: 15,000 tokens
  Agent B: 15,000 tokens  ← duplicate loading
  Agent C: 15,000 tokens  ← duplicate loading
  Total: 45,000 tokens

With Graph Context (3 agents sharing index):
  Shared index: built once
  Agent A snapshot: ~2,000 tokens (only relevant chunks)
  Agent B snapshot: ~2,000 tokens
  Agent C snapshot: ~2,000 tokens
  Total: ~6,000 tokens
  Savings: 87%+
```

## Why Zero Extra Resources

| Comparison | Vector DB Approach | Graph Context |
|-----------|-------------------|---------------|
| External dependency | Requires vector DB deployment | None |
| GPU | Embedding model needs GPU | None |
| Network calls | Embedding API calls | None |
| Memory | Large vector index | Lightweight (BM25 inverted index) |
| Cold start | Pre-compute embeddings | Seconds-level AST parsing |
| Accuracy | Semantically similar but imprecise | AST-level exact match |

## Supported Languages

**Code parsing (AST):** Python, JavaScript, TypeScript, JSX, TSX, Vue, Svelte, Go, Rust, Java, Kotlin, C, C++, Shell

**Query languages:** Chinese, English (built-in CN/EN synonym mapping, zero-cost semantic bridging)

## Project Structure

```
graph-context/
├── graph_context/
│   ├── __init__.py          # Package entry
│   ├── __main__.py          # python -m graph_context
│   ├── engine.py            # Core engine (AST + BM25 + graph diffusion)
│   ├── server.py            # MCP Server (25+ tools)
│   ├── rules.py             # Self-evolving rules engine
│   ├── project_scope.py     # Multi-project scope management
│   ├── synonyms.py          # CN/EN synonym mapping
│   ├── ground_truth.py      # Ground truth evaluation
│   └── ground_truth_report.py
├── tests/
│   ├── experiment.py        # Integration tests
│   ├── stress_test.py       # Stress tests
│   └── token_simulation.py  # Token consumption simulator
├── pyproject.toml
├── LICENSE
└── README.md
```

## Running Tests

```bash
# Integration tests
python -m tests.experiment

# Stress tests
python -m tests.stress_test

# Token consumption simulation (generates report)
python -m tests.token_simulation
```

## License

[MIT](LICENSE)

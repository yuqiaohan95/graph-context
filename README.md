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

## MCP Tools — Dynamic Loading

**v6 architecture:** Only 2 tools are injected at startup (~270 tokens). Other tools are loaded on demand via the `tools` manager, saving context tokens in every conversation turn.

| Phase | Tools | Token Cost |
|-------|-------|-----------|
| Startup | `search` + `tools` | ~270 tokens |
| + `rules` module | +6 tools | ~770 tokens total |
| + `admin` module | +11 tools | ~1,670 tokens total |
| All tools (legacy) | 25+ tools | ~3,000-4,000 tokens |

### Always Loaded (Startup)

#### `search` — Code Retrieval

Search project code using AST call graph + BM25 ranking.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | string | required | Natural language or code term to search for |
| `top_k` | int | 5 | Number of results (max 20) |
| `mode` | string | `"graph"` | `"graph"` (BM25 + diffusion) or `"deps"` (+ cross-file dependencies) |

#### `tools` — Dynamic Tool Manager

Load/unload tool modules on demand to save context tokens.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `action` | string | `"list"` | `list` / `load` / `unload` / `loaded` |
| `module` | string | `""` | Module name to load/unload |

**Usage example (LLM calls):**
```
tools(action="list")                          # See available modules
tools(action="load", module="rules")          # Load rules tools
tools(action="unload", module="rules")        # Unload when done
tools(action="loaded")                        # See what's currently loaded
```

### Module: `rules` — Self-evolving Rules Engine

Load with: `tools(action="load", module="rules")`

| Tool | Description |
|------|-------------|
| `rules_list` | List retrieval rules (filter by type/scope/confidence/status) |
| `rules_add` | Add a new rule (auto-enters observation period) |
| `rules_apply` | Apply a rule (records a hit) |
| `rules_verify` | Verify a rule as correct/incorrect (drives accuracy-based decay) |
| `rules_prune` | Prune low-performing rules |
| `rules_discover` | Auto-discover candidate rules from retrieval errors |

### Module: `admin` — Engine Config, Snapshots, Scopes

Load with: `tools(action="load", module="admin")`

| Tool | Description |
|------|-------------|
| `admin_health` | Engine health check (index stats, watcher, cache, MVCC version) |
| `admin_config` | Get current engine configuration |
| `admin_update_config` | Update config at runtime |
| `snapshot_create` | Create MVCC snapshot (COW, no deepcopy) |
| `snapshot_read` | Read snapshot at a specific version |
| `snapshot_status` | Get MVCC status |
| `scope_create` | Create project scope (strict isolation / shared) |
| `scope_list` | List all project scopes |
| `scope_link` | Link two projects |
| `synonym_add` | Add Chinese-English synonym mapping |
| `synonym_discover` | Discover candidate synonyms from unmatched query tokens |

### Resources

| URI | Description |
|-----|-------------|
| `context://stats` | Engine statistics (chunks, edges, cache) |

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

### Tool Definition Overhead

Every conversation turn carries tool definitions as context. Dynamic loading drastically reduces this cost:

```
Legacy (all 25+ tools injected):
  Every turn: ~3,500 tokens for tool definitions
  10-turn conversation: ~35,000 tokens wasted on tool definitions alone

Dynamic loading (v6):
  Startup: ~270 tokens (search + tools)
  10-turn conversation (search only): ~2,700 tokens
  10-turn conversation (search + rules): ~7,700 tokens
  Savings: 78-92% on tool definition overhead
```

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

## Benchmark Results

Tested with the built-in token simulation (`python -m tests.token_simulation`):

| Project Size | BM25 Recall | BM25 + Rules | Token Savings | Rules Created |
|-------------|------------|-------------|--------------|--------------|
| Small (4 modules) | 100% | 100% | 76.3% | 0 |
| Medium (10 modules) | 85% → 100% | 100% | 88.7% | 4 |
| Large (14 modules) | 80% → 100% | 97% | 90.7% | 6 |

Cost comparison (Large project, Claude 3.5 Sonnet):

| Scenario | Cost |
|----------|------|
| Without MCP | ¥14.83 |
| With MCP (with rules) | ¥3.30 |
| **Savings** | **78%** |

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
│   ├── server_consolidated.py  # MCP Server (dynamic loading, 2 base + on-demand modules)
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

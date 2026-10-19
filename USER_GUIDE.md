# Comprehensive User Guide: CodeGraph Brain Enterprise
### (Universal Code Knowledge Graph, Polyglot GraphRAG & AI Architectural Review Platform)

---

## Table of Contents
1. [Introduction & Architectural Overview](#1-introduction--architectural-overview)
2. [Prerequisites & Docker Deployment](#2-prerequisites--docker-deployment)
3. [Dashboard Walkthrough & Navigation](#3-dashboard-walkthrough--navigation)
4. [Configuring Local AI / LLM Providers](#4-configuring-local-ai--llm-providers)
5. [Interactive Architecture Chat (GraphRAG & NL2Cypher)](#5-interactive-architecture-chat-graphrag--nl2cypher)
6. [Code Clones & Git Hotspots Discovery](#6-code-clones--git-hotspots-discovery)
7. [Automated Documentation Engine (Auto-Doc)](#7-automated-documentation-engine-auto-doc)
8. [Pull Request Diff Review & Blast Radius Analysis](#8-pull-request-diff-review--blast-radius-analysis)
9. [Microservices & Test Traceability](#9-microservices--test-traceability)
10. [Model Context Protocol (MCP) Integration](#10-model-context-protocol-mcp-integration)
11. [REST API Documentation & Key Endpoints](#11-rest-api-documentation--key-endpoints)

---

## 1. Introduction & Architectural Overview

**CodeGraph Brain Enterprise** is an enterprise-grade platform designed to reverse-engineer software architectures and construct a **hybrid relational and semantic code knowledge graph**.

### Core Platform Capabilities:
- **Polyglot AST Engine:** Parses diverse programming languages including Python, JavaScript/TypeScript, Go, Java, C, C++, C#, and Rust using Tree-sitter.
- **Unified Knowledge Graph:** Integrates structural AST hierarchies with 768-dimensional code embeddings in Neo4j.
- **Dynamic AI Configuration:** Seamlessly connects to local LLM engines (Ollama, vLLM, LM Studio) or enterprise endpoints directly from the UI without service restarts.
- **Architecture Health & Code Smell Diagnostics:** Pinpoints circular dependency chains, god functions, bottlenecks, dead code, duplicates (clones), and bug hotspots.
- **PR Risk & Blast Radius Review:** Evaluates code modifications in pull requests up to 3 hops deep to predict breaking impacts and guide regression testing.
- **Automated Docstring Generation:** Dispatches asynchronous workers to enrich undocumented functions with standardized docstrings.
- **Natural Language to Graph Queries (NL2Cypher):** Translates free-form questions into precise Cypher graph queries and executes them safely against Neo4j.

---

## 2. Prerequisites & Docker Deployment

### Requirements:
- **Docker** and **Docker Compose**
- Minimum 4 GB of available system memory
- Local **Ollama** instance running with the code embedding model:
  `jina-embeddings-v2-code:latest`

> This embedding model generates standardized 768-dimensional vectors tailored for programming source code.

### Deployment Commands:
```bash
git clone https://github.com/mohamadreza1368/code_brian.git
cd code_brian
cp .env.example .env
docker compose up -d --build
```
> The dashboard will be available at `http://localhost:8001`.

---

## 3. Dashboard Walkthrough & Navigation

The platform features six primary operational views:
1. 🌐 **Knowledge Graph:** Interactive force-directed canvas (Vis.js / Cytoscape) to inspect symbol relationships, code clones, and API route links.
2. 💬 **AI Chat:** Interactive architectural assistant supporting GraphRAG retrieval, direct code assistance, and natural language Cypher execution.
3. 📊 **Architecture Health:** Specialized panel to detect code clones, dead code candidates, high-risk hubs, and trigger background Auto-Doc workers.
4. 📁 **Repositories Hub:** Manage indexed repositories, clone remote Git URLs, or upload zip archives.
5. 🔍 **Diff Impact & PR Review:** Paste Git diffs to compute blast radius, identify affected callers, and receive AI-guided risk reports.
6. 🤖 **MCP Agent Connect:** Copy ready-to-use configuration blocks to connect Cursor, Claude Desktop, or custom AI coding agents.

---

## 4. Configuring Local AI / LLM Providers

CodeGraph Brain Enterprise is engineered to operate **100% offline and air-gapped**:
1. In the web dashboard, click the purple **"LLM Settings"** button in the top navigation bar.
2. Enter your LLM provider base URL (e.g. for Ollama on the host machine: `http://host.docker.internal:11434/v1`).
3. Enter your model name (e.g. `llama3`, `qwen2.5-coder`, or `mistral`).
4. Enter an API key if using an enterprise or authenticated provider (optional for local Ollama).
5. Click **"Save Settings"**.

*All subsequent operations—including GraphRAG, PR Reviews, and Auto-Doc—will immediately utilize the configured model.*

---

## 5. Interactive Architecture Chat (GraphRAG & NL2Cypher)

The **AI Chat** interface offers three distinct operational modes:
- **GraphRAG Mode (Recommended):** Combines vector similarity search with 2-hop relational graph traversal to answer questions with verifiable structural context (e.g., *"How is authentication implemented across services?"*).
- **Direct Mode:** Direct chat with the LLM for general software engineering queries, code snippet formatting, and algorithmic logic.
- **Natural Language to Cypher (NL2Cypher):** Prefix your query with `/cypher` to generate and run real-time graph queries.
  *Example:* `/cypher find all functions in auth.py that call the database`
  The model inspects schema metadata, synthesizes a read-only Cypher query, executes it against Neo4j, and returns verified facts.

---

## 6. Code Clones & Git Hotspots Discovery

### Code Clones (Duplicate Logic)
Under the **Architecture Health** view, click **"Find Clones"**. The engine computes cosine similarity across all function embedding vectors in the repository and displays pairs with $\ge 95\%$ semantic similarity, allowing you to eliminate redundant implementations.

### Git Hotspots & High Churn
Click **"Git Hotspots"** to inspect files with high commit frequency (churn rate). Modules that undergo frequent modifications while maintaining high structural fan-in/fan-out are flagged as high-risk hotspots requiring regression test suites.

---

## 7. Automated Documentation Engine (Auto-Doc)

1. Navigate to **Architecture Health** and click the green **"Run Auto-Doc"** button.
2. An asynchronous Celery worker scans the codebase for functions lacking docstrings or explanatory comments.
3. The worker queries your configured LLM to generate descriptive docstrings adhering to standard formatting conventions (e.g., Google or Sphinx style) and writes them back into the graph database.

---

## 8. Pull Request Diff Review & Blast Radius Analysis

1. Generate a Git diff for your proposed changes:
   ```bash
   git diff main...feature-branch
   ```
2. Paste the diff output into the **Diff Impact & PR Review** view.
3. The platform parses modified files and functions, traces incoming and outgoing dependency paths up to 3 hops deep, and generates an automated risk assessment with regression testing suggestions.

---

## 9. Microservices & Test Traceability

The ingestion engine establishes architectural links across services:
- **API Endpoints:** Handlers such as `@app.get` in FastAPI, `router.post` in Express, and route annotations are indexed as `ApiRoute` nodes with `EXPOSES_API` relationships.
- **Frontend Clients:** HTTP calls (`fetch`, `axios.get`, `axios.post`) are linked to corresponding `ApiRoute` nodes via `CALLS_API`.
- **Test Coverage:** Automated test files and test suites are mapped to target implementation classes and functions via `TESTS` relationships.

---

## 10. Model Context Protocol (MCP) Integration

The platform includes a dedicated FastMCP server over `stdio`. To connect AI coding assistants such as Cursor or Claude Desktop, insert the following configuration block into your MCP client settings:

```json
{
  "mcpServers": {
    "codegraph-brain": {
      "command": "docker",
      "args": [
        "exec",
        "-i",
        "graphrag_mcp",
        "python",
        "-m",
        "app.mcp_server",
        "--transport",
        "stdio"
      ]
    }
  }
}
```

### Available MCP Tools:
- `search_code`: Perform hybrid vector and symbol search over the codebase.
- `get_call_hierarchy`: Retrieve callers and callees for any method.
- `calculate_blast_radius`: Analyze breaking change risks before editing a function.
- `detect_code_smells`: Audit God functions, circular dependencies, and dead code.
- `execute_cypher`: Execute verified read-only queries directly against the graph database.

---

## 11. REST API Documentation & Key Endpoints

Interactive Swagger documentation is available at `http://localhost:8001/docs`. Common API operations:

```bash
# Update LLM configuration
curl -X POST "http://localhost:8001/api/v1/config/llm" \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"base_url": "http://host.docker.internal:11434/v1", "model": "llama3", "api_key": ""}'

# Retrieve detected code clones
curl -X GET "http://localhost:8001/api/v1/analytics/clones?repo_name=MyProject" \
  -H "X-API-Key: your-api-key"

# Trigger background Auto-Doc task
curl -X POST "http://localhost:8001/api/v1/tasks/autodoc" \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"repo_name": "MyProject"}'
```

---

<div align="center">
  <sub>CodeGraph Brain Enterprise — Built for software architects, security leads, and engineering teams.</sub>
</div>

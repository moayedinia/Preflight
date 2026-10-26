# Comprehensive User Guide: Preflight (Vibe Coder Edition)
### Autonomous Code Quality, Preflight Security & AI Copilot for Fast-Shipping Developers

---

## Table of Contents
1. [Introduction & Purpose](#1-introduction--purpose)
2. [Prerequisites & Quick Docker Setup](#2-prerequisites--quick-docker-setup)
3. [The 3-Tier Preflight Auditor (Ready to Ship)](#3-the-3-tier-preflight-auditor-ready-to-ship)
4. [Fixing Issues with 1-Click AI Prompts](#4-fixing-issues-with-1-click-ai-prompts)
5. [AI Codebase Copilot & Architectural Q&A](#5-ai-codebase-copilot--architectural-qa)
6. [1-Click Ship Assets (README & .env.example)](#6-1-click-ship-assets-readme--envexample)
7. [AI Provider Configuration (OpenAI / OpenRouter / Local)](#7-ai-provider-configuration-openai--openrouter--local)
8. [MCP Integration (Cursor & Claude Desktop)](#8-mcp-integration-cursor--claude-desktop)
9. [REST API Documentation & Endpoints](#9-rest-api-documentation--endpoints)

---

## 1. Introduction & Purpose

**Preflight** is a specialized code audit and shipping assistant designed for **Vibe Coders**, indie hackers, and developers building applications with AI coding tools (such as Cursor, Claude 3.5 Sonnet, Lovable, Bolt.new, v0, and ChatGPT).

### Why Vibe Coders Need Preflight:
When prototyping quickly with AI:
- API keys (OpenAI, Stripe, AWS) often get accidentally left in frontend source files.
- Buttons sometimes have dummy or empty event handlers (`console.log('clicked')`).
- Async forms lack loading spinners, leading to double charges or multiple submissions.
- Route handlers miss proper error handling, causing fatal unhandled 500 crashes.

Preflight scans your entire codebase, calculates an instant **Ship Readiness Score (0-100%)**, groups problems into clear severity tiers, and gives you **one-click copyable prompts** tailored specifically to paste into your AI assistant to fix the code automatically.

---

## 2. Prerequisites & Quick Docker Setup

### Requirements:
- **Docker** and **Docker Compose**
- Runs on any standard machine (Windows, macOS, Linux) with < 1 GB RAM.
- **No GPU or CUDA required.**

### Launching the Stack:
```bash
# 1. Clone the repository
git clone https://github.com/mohamadreza1368/Vultr_Hackathon.git
cd Vultr_Hackathon

# 2. Configure environment (optional: add your API key)
cp .env.example .env

# 3. Start the containers
docker compose up -d --build
```

### Accessing the Services:
- 🌐 **Web Dashboard:** `http://localhost:8001`
- 📚 **Swagger Interactive API Docs:** `http://localhost:8001/docs`
- 🤖 **Model Context Protocol (MCP) Server:** `http://localhost:8002`

---

## 3. The 3-Tier Preflight Auditor (Ready to Ship)

In the **Ship Audit** tab, click **"Run Full Audit"** to execute deep AST and semantic security checks across your project:

### 🔴 Critical Blockers (Red Tier)
Must be resolved before publishing to users or production:
- **Leaked API Keys & Secrets:** Hardcoded keys for OpenAI, Stripe, AWS, GitHub, etc.
- **Unprotected Sensitive Routes:** Admin or mutation endpoints accessible without authentication.
- **SQL / NoSQL Injections:** Dangerous unescaped query string interpolations.
- **Fatal Server Crashes:** Endpoints missing `try/catch` or error handlers.
- **Dead Core Buttons:** Checkout, payment, or submit buttons with mock or empty handlers.

### 🟠 UX & Reliability (Orange Tier)
Operational hazards that lead to poor user experience:
- **Missing Loading State:** Submit buttons that can be clicked multiple times while an async request is in-flight (risk of double-billing).
- **Unbounded Input Fields:** Form inputs or textareas accepting unlimited payload length without validation.
- **Un-debounced Searches:** Search bars firing API requests on every single keystroke.

### 🟡 Code Hygiene & Polish (Yellow Tier)
Best practices for publishing a clean product:
- **Stray Debug Logs:** Forgotten `console.log()` or debugger statements in production bundles.
- **Missing Documentation:** Empty or placeholder `README.md`.
- **Accessibility & Contrast:** Missing essential button and input labels.

---

## 4. Fixing Issues with 1-Click AI Prompts

For each detected issue in the dashboard:
1. Read the **Plain English Explanation** (what is wrong in simple non-technical terms).
2. Review the **Why this matters** explanation (security or business consequence).
3. Click the **"📋 Copy Fix Prompt for AI"** button.
4. Switch to your AI code editor (**Cursor**, **Claude**, **Lovable**, or **Bolt**) and paste the prompt.
5. The AI assistant will instantly rewrite the offending snippet with a safe, production-grade fix!

---

## 5. AI Codebase Copilot & Architectural Q&A

Click the **AI Copilot** tab to chat directly with an assistant that understands your full project structure:
- Grounded in AST syntax trees and symbol dependencies to prevent hallucinations.
- Click ready-to-use quick chips like:
  - *"How does authentication work here?"*
  - *"List all external API calls in this project"*
  - *"What environment variables does this need to deploy?"*

---

## 6. 1-Click Ship Assets (README & .env.example)

In the **Ship Assets** tab, generate polished deliverables with a single click:
- **Polished `README.md`:** Analyzes project dependencies, detected frameworks, and route handlers to craft a complete GitHub repository README with overview, setup instructions, and feature lists.
- **Safe `.env.example`:** Automatically extracts every referenced environment variable across Python, JavaScript, TypeScript, and Go files, producing a clean template without exposing real secret values.
- **Export Report (.md):** Download the full preflight audit report in Markdown format for client handovers or compliance checks.

---

## 7. AI Provider Configuration (OpenAI / OpenRouter / Local)

Preflight supports any OpenAI-compatible provider:
1. Click the **Settings (⚙️)** icon in the top-right corner of the dashboard.
2. Enter your **Base URL** (e.g. `https://api.openai.com/v1`, `https://openrouter.ai/api/v1`, or `http://localhost:11434/v1` for local Ollama).
3. Enter your **Model Name** (e.g. `gpt-4o-mini`, `claude-3-5-sonnet`, `deepseek-chat`, etc.).
4. Enter your **API Key**.
5. Click **"Test Connection"** to verify reachability, then click **"Save Settings"**.

> **Offline / Fallback Mode:** If no API key is provided, Preflight automatically operates in fallback mode, using normalized deterministic vectors so you can still index and inspect projects offline.

---

## 8. MCP Integration (Cursor & Claude Desktop)

Preflight includes a built-in **Model Context Protocol (MCP)** server on port `8002`.

### Configuration for Cursor / Claude Desktop:
Add the following snippet to your MCP config:
```json
{
  "mcpServers": {
    "preflight": {
      "url": "http://localhost:8002/sse"
    }
  }
}
```

Now your AI assistant in Cursor or Claude Desktop can directly invoke Preflight tools to audit code, inspect blast radius, and explore repository architecture.

---

## 9. REST API Documentation & Endpoints

All endpoints are documented interactively at `http://localhost:8001/docs`:

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/healthz` | System health check (Neo4j, Redis) |
| `GET` | `/api/v1/repos` | List all indexed projects |
| `POST` | `/api/v1/ingest/git` | Asynchronously clone and index a Git repository |
| `POST` | `/api/v1/ingest/upload` | Upload and index a ZIP archive |
| `GET` | `/api/v1/tasks/{taskId}` | Poll asynchronous ingestion task progress |
| `GET` | `/api/v1/review/tier/{tier}` | Run preflight defect audit for a specific tier (`red`, `orange`, `yellow`) |
| `POST` | `/api/v1/chat` | Send a query to the AI Copilot |
| `GET` | `/api/v1/config/llm` | Retrieve active LLM configuration |
| `POST` | `/api/v1/config/llm` | Update LLM configuration |
| `GET` | `/api/v1/config/embedding` | Retrieve embedding engine configuration |
| `POST` | `/api/v1/config/embedding` | Update embedding engine configuration |

---

*Preflight — Build with AI, Ship with Confidence.*

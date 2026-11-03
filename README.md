# 🚀 Preflight — Autonomous Code Quality & Shipping Auditor for Vibe Coders

[![License: MIT](https://img.shields.io/badge/License-MIT-emerald.svg)](LICENSE)
[![Docker Ready](https://img.shields.io/badge/Docker-Compose-2496ED.svg?logo=docker&logoColor=white)](docker-compose.yml)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688.svg?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Neo4j](https://img.shields.io/badge/Neo4j-5.11+-008CC1.svg?logo=neo4j&logoColor=white)](https://neo4j.com)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)

**Preflight** is a lightweight, AI-driven code quality and preflight audit platform specifically tailored for **Vibe Coders**, indie hackers, and developers using AI assistants (Cursor, Claude, Lovable, Bolt, v0).

Before you publish or ship your app, Preflight inspects your codebase, calculates a **Ship Readiness Score**, flags critical blockers (leaked secrets, broken buttons, missing loading states), and provides **1-click copyable prompts** to fix issues instantly in your favorite AI code editor!

---

## 🌟 Key Features for Vibe Coders

### 1. 🛡️ Autonomous 3-Tier Preflight Auditor
- 🔴 **Critical Blockers:** Scans for hardcoded API keys (OpenAI, Stripe, AWS), unprotected admin endpoints, SQL/NoSQL injection, and dead/mock button clicks.
- 🟠 **UX & Operational Hazards:** Identifies missing loading states during async actions (preventing accidental double-payments), un-debounced inputs, and unbounded text fields.
- 🟡 **Code Hygiene & Polish:** Catches stray `console.log` / debug statements, missing documentation, and contrast issues.
- 📋 **1-Click AI Fix Prompts:** For every detected issue, click **"Copy Fix Prompt"** to get a ready-made, precise instruction to paste into Cursor, Claude, or Lovable to fix the code automatically.

### 2. ⚡ Lightweight & GPU-Free (API-First)
- **No GPU Required:** Removed heavy local vLLM containers. Runs fast on any laptop or small server with < 1GB RAM.
- **Universal AI Provider Support:** Connects directly via standard OpenAI-compatible API (OpenAI, OpenRouter, Groq, Ollama) or runs smoothly in offline fallback mode without an API key.

### 3. 💬 AI Codebase Copilot
- Ask structural questions about your codebase (*"How does user authentication work?"*, *"What environment variables are needed?"*).
- Grounded in AST syntax and symbol dependency graphs to prevent hallucinations.

### 4. 📦 1-Click Ship Deliverables
- **Auto-Generated `README.md`:** Crafts a professional, beautiful repository README based on actual detected routes and functions.
- **Safe `.env.example` Extraction:** Scans all codebase environment references and exports a sanitized template ready for deployment.
- **Export Audit Report:** Download comprehensive Markdown audit reports for clients or team reviews.

---

## 🛠️ Quick Start with Docker

```bash
# 1. Clone the repository
git clone https://github.com/mohamadreza1368/Vultr_Hackathon.git
cd Vultr_Hackathon

# 2. Configure environment variables (optional: add your OpenAI key)
cp .env.example .env

# 3. Launch the lightweight stack
docker compose up -d --build
```

Access the dashboard:
- 🌐 **Web UI Dashboard:** `http://localhost:8001`
- 📚 **Swagger API Docs:** `http://localhost:8001/docs`
- 🤖 **MCP SSE Server for Cursor/Claude Desktop:** `http://localhost:8002`

### Isolated project previews

Reviewed projects are not executed inside the API container. A private
`sandbox_runner` service creates a short-lived container for each preview and
selects a runtime image for Node.js, Python, .NET, Maven, Gradle, Go, Rust, PHP,
or static sites. Source files are copied to a disposable Docker volume; the
reviewed repository is never mounted directly into the execution container.

The runner applies memory, CPU, and process limits, drops Linux capabilities,
and attaches previews to a network separate from Neo4j and Redis. Preview
traffic is available only through the API reverse proxy, so no dynamic host
port range is required.

The runner requires access to the local Docker Engine through
`/var/run/docker.sock`. Configure a strong `SANDBOX_RUNNER_TOKEN` in `.env`
before deploying outside a local development machine. The first launch for a
language may take longer while its runtime image is downloaded.

---

## ⚡ Vultr Cloud Architecture & Standards Integration

Preflight is architected according to official Vultr developer and marketplace standards:

### 1. ⚡ Vultr Serverless Inference Integration
- **Direct OpenAI-Compatible Gateway:** `https://api.vultrinference.com/v1`
- **Dynamic Model Catalog:** Supports live model fetching and execution across 35+ Vultr-hosted models including `deepseek-v4.1-flash-1017`, `glm-5.3`, `mimo-v2.6-pro`, `qwen3.8-27b`, and `qwen3-embedding-4b`.
- **1-Click UI Preset:** Switch AI providers to Vultr Serverless Inference with one click in the Settings modal.

### 2. 🛡️ Vultr Agent Sandboxing & E2B / OpenSandbox Standards
- Complies with [Vultr Cloud Compute Agent Sandboxing](https://docs.vultr.com/how-to-set-up-agent-sandboxing-on-vultr-cloud-compute) hardening:
  - **Memory Quota:** `1GB` RAM isolation.
  - **CPU Quota:** `1.0 CPU` (`10^9` nano-CPUs).
  - **PID Limits:** `100` processes (fork bomb immunity).
  - **Security Capabilities:** `cap_drop=["ALL"]`, `no-new-privileges:true`.
  - **MicroVM Cloud Support:** Integrated `CloudSandboxDriver` compatible with E2B microVMs and OpenSandbox protocol.

### 3. 🚀 Vultr Coolify Marketplace 1-Click Deployment
- Deploy Preflight on any [Vultr Coolify Marketplace Instance](https://docs.vultr.com/how-to-deploy-an-application-with-vultr-coolify-marketplace-app) using `docker-compose.coolify.yml`.
- Includes Traefik SSL auto-provisioning, internal bridge networking, and persistent healthchecks.
```bash
# 1-Click Deploy on Vultr Coolify
docker compose -f docker-compose.coolify.yml up -d --build
```

### 4. 🗄️ Vultr Supabase Marketplace Integration
- Connects directly to self-hosted [Vultr Supabase Marketplace Instances](https://docs.vultr.com/how-to-deploy-a-nextjs-application-with-vultr-supabase-marketplace-app).
- Automatically syncs Orca Code Review reports and audit artifacts to Supabase Storage and enables PostgreSQL / pgvector embeddings sync.

---

## ⚙️ Environment Configuration

Edit `.env` to configure your AI provider (optional):
```env
# Optional: Set your OpenAI or OpenRouter API key
OPENAI_API_KEY=sk-...
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
```
*Note: If no API key is provided, the platform automatically runs in offline fallback mode.*

---

## 📄 License
This project is open-source under the MIT License.

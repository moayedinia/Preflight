# Preflight measurements, 2026-11-05

> **Scope note.** These measurements were taken on a local snapshot that predates `main` at `99989f3`. Twenty files differ, including `sandbox.py`, `sandbox_runner.py`, `docker-compose.yml`, `llm.py`, `preflight.py`, `main.py` and `README.md`. A static check of `99989f3` shows:
> - **Fixed since the snapshot:** the README now documents 256m / 0.5 CPU / 64 PIDs (finding 1). The `api` service now runs as `user: root`, so the socket permission failure in the Docker addendum does not apply to `99989f3`.
> - **Still present in `99989f3` (static check, not re-measured):** the capabilities dict reads `SANDBOX_CPU_LIMIT` while the runner reads `SANDBOX_NANO_CPUS`, and flags are hard-coded `"VERIFIED"`. The host-process fallback guard fires only when `SANDBOX_RUNNER_URL` is unset. The default runner token is `change-me-in-production`. `preflight_sandbox_net` has no `internal: true`. The embedder defaults to `https://api.openai.com/v1`. Substring routing is unchanged. `require_user` is used by 0 routes in `main.py`. `GET /api/v1/config/llm` still returns `get_llm_config()` unmasked.
> - **Not re-run against `99989f3`:** the test suite counts, the scoring outputs, and the Docker probe results.

Scope: everything measurable without a model call. **No request was made to any model provider.** Every script ran under a socket guard that blocks all non-loopback connections and all non-local DNS, and logs each attempt. Section 7 lists every attempt the guard caught (none reached a model provider).

Environment: macOS (Darwin 25.6.0), Python 3.11.18 venv built from `backend/requirements.txt` (fastapi 0.162.0, openai 1.59.9, docker-py 7.2.0). **No Docker engine is installed on this machine** (no `docker` binary, no `/var/run/docker.sock`, no Colima, OrbStack, Podman or Lima). **No Node.js** either.

Measurement scripts are in the session scratchpad. The one repo file added is `eval/containment_probe.py` (Group 2 probe, not yet executed). No test, case file, prompt or app source was edited.

---

## Top findings (read these first)

1. **The README, the capabilities dictionary and the runner disagree on the containment numbers.** README: 1 GB RAM, 1.0 CPU, 100 PIDs. Dictionary and runner: 256 MiB, 0.5 CPU, 64 PIDs. If the submission quotes the README, it quotes numbers the code never requests.
2. **The capability claim cannot track reality.** The CPU and network claims come from env vars the runner never reads. Measured: with `SANDBOX_NANO_CPUS=2000000000 SANDBOX_DOCKER_NETWORK=bridge`, the runner requests 2.0 CPUs on Docker's default `bridge`, while `/api/v1/sandbox/capabilities` still returns `"0.5 CPU"` and `"isolated_bridge"`.
3. **There is no separate sandbox_runner service.** Both compose files define only `api` and `frontend`. The runner is mounted inside the public API process at `/runner` (`backend/app/main.py:258-261`). That process holds `/var/run/docker.sock` and also sits on `preflight_sandbox_net`. Measured: `POST /runner/sessions/start` with the compose default token `change-me-in-production` gets past auth and stops only at "Docker daemon unavailable". On a Docker host it would create a container running a caller-supplied command.
4. **The "isolated" sandbox network is a plain bridge.** `preflight_sandbox_net` is `driver: bridge` with no `internal: true`, and the API container is attached to it. By configuration, sandboxes have outbound internet and can reach `api:8000`, which serves the runner.
5. **Your Vultr key is sent to OpenAI for embeddings.** Under the compose environment, `get_embedding()` POSTs to `https://api.openai.com/v1/embeddings` with `Authorization: Bearer <LLM_API_KEY>`. I intercepted the request before it was sent (Group 5).
6. **The scoring router misroutes findings.** An empty or one-letter rule is scored as a 40-point "Leaked API key". An auth bypass whose text contains "search and sort are wrong" gets a 12-point Correctness deduction. Non-keyword security findings go to Reliability.
7. **A no-key scan says "Ready to launch... Go." for a repo with a hardcoded `sk-proj-` key and string-built SQL.** Both were detected, then left unconfirmed because no sandbox confirmation can run, so they deduct 0.
8. **19 tests fail on the shipped code** (auth, CSRF, ownership, key masking). These are real, not test-mode artefacts: `require_user` is wired to 0 routes in `main.py`, and `GET /api/v1/config/llm` returns the raw API key.

---

## Group 1: containment, claimed vs actually applied

**APPLIED: NOT MEASURED.** Blocked because no Docker engine is installed, so no container exists for `docker inspect`.

Closest available evidence: I drove the real runner code path (`app.sandbox_runner.start`) with docker-py's daemon transport intercepted. That captures the exact JSON body docker-py would POST to `/v1.43/containers/create`. The **REQUESTED** column is what the runner asks Docker for. It is not proof that Docker applied it. Settings are the env values from `docker-compose.yml`.

Captured request:
```
POST http+docker://localhost/v1.43/containers/create
HostConfig: {"Binds": ["preflight-sandbox-probeapp-workspace:/workspace:rw"], "CapDrop": ["ALL"],
 "Memory": 268435456, "NanoCpus": 500000000, "NetworkMode": "preflight_sandbox_net", "PidsLimit": 64,
 "ReadonlyRootfs": true, "SecurityOpt": ["no-new-privileges:true"],
 "Tmpfs": {"/home/node/.npm": "rw,size=64m", "/root/.cache": "rw,size=64m", "/tmp": "rw,noexec,nosuid,size=128m"}}
Config.User: 1000:1000   Image: python:3.11-slim
```

| # | Item | CLAIMED (`get_sandbox_capabilities()`) | README claim | REQUESTED (runner, captured) | APPLIED (Docker) | MATCH |
|---|---|---|---|---|---|---|
| 1.1 | ReadonlyRootfs | not claimed (no key in dict) | not mentioned | `true` | NOT MEASURED | n/a, claim absent |
| 1.2 | Memory | `"256m"`, `--memory=256m` | **1 GB**; env table default `1g` | 268435456 bytes = 256 MiB | NOT MEASURED | dict = request; **README MISMATCH** |
| 1.3 | NanoCpus | `"0.5 CPU"`, `--cpus=0.5` (from `SANDBOX_CPU_LIMIT`) | **1.0 CPU** (10^9) | 500000000 = 0.5 CPU (from `SANDBOX_NANO_CPUS`) | NOT MEASURED | equal only at defaults. **MISMATCH under config change** (measured: request 2000000000, claim still 0.5). **README MISMATCH.** Flag text also says "strictly to 1.0 virtual core" (`sandbox.py:177`) |
| 1.4 | PidsLimit | `64` | **100** | `64` | NOT MEASURED | dict = request; **README MISMATCH** |
| 1.5 | CapDrop | `["ALL"]` (literal) | `cap_drop=["ALL"]` | `["ALL"]` (literal) | NOT MEASURED | match (both hard-coded) |
| 1.6 | SecurityOpt | `["no-new-privileges:true"]` (literal) | same | `["no-new-privileges:true"]` | NOT MEASURED | match (both hard-coded) |
| 1.7 | NetworkMode | `"isolated_bridge"` (from `SANDBOX_NETWORK_MODE`, unset everywhere, so always the default string) | "isolated internal networks" (Coolify) | `preflight_sandbox_net` (from `SANDBOX_DOCKER_NETWORK`) | NOT MEASURED | **MISMATCH.** "isolated_bridge" is not a Docker network. The real network is a plain `bridge` with no `internal: true`, shared with the `api` container. Measured: with `SANDBOX_DOCKER_NETWORK=bridge` the request is `"bridge"` and the claim is unchanged |
| 1.8 | Config.User | not claimed | not mentioned | `1000:1000`, non-root UID | NOT MEASURED | n/a, claim absent |
| 1.9 | Tmpfs | not claimed | not mentioned | `/tmp rw,noexec,nosuid,size=128m`; `/root/.cache rw,size=64m`; `/home/node/.npm rw,size=64m` (last two lack noexec and nosuid) | NOT MEASURED | n/a, claim absent |

Other facts about the claim:
- Every flag in `CONTAINMENT_FLAGS` carries `"status": "VERIFIED", "verified": True` as literals, and `get_verifiable_containment_flags()` returns `"all_flags_enforced": True` as a literal (`sandbox.py:129-201`). No code path reads a container to set them.
- `/api/v1/sandbox/capabilities` and `/api/v1/sandbox/verifiable-flags` are the same handler with no auth dependency. Measured: HTTP 200 with no credentials.
- The runner already has a real readback, `Session.status()["applied_containment"]` (`sandbox_runner.py:120-138`), which reads `container.attrs["HostConfig"]`. Nothing feeds it into the capabilities endpoint. Its `memory_human` and `cpus_human` fields are not readbacks: one is the env string, the other the literal `"0.5 CPU"`.
- `test_sandbox_capabilities_vultr_hardened` (`tests/test_vultr_standards.py:57`) asserts against the dictionary only. No test in the suite inspects the arguments passed to `containers.create`.
- `tests/test_checklist_verification.py::test_item9_two_services_in_compose` asserts the compose file has exactly `{"api", "frontend"}`, so the suite enforces that no separate runner service exists.

---

## Group 2: containment probes

**All eleven probes: NOT MEASURED.** Blocked because no Docker engine is installed, so no sandbox can be created through the scan path. **Contained count: NOT MEASURED (0 of 11 attempted).** No verbatim kernel messages exist, and I have not substituted any.

Probe script: `eval/containment_probe.py` (stdlib Python for the `python:3.11-slim` runtime). It compiles. It has never run in a container. Its docstring holds the run instructions. Each probe runs in a child process and prints the exact exception text. The fork bomb is capped at 500 children and the memory balloon at 768 MB, so the control run cannot hurt the host.

Configuration facts that bear on the probes (code reading, **not results**):

| # | Probe | Relevant configuration fact | Result |
|---|---|---|---|
| 2.1-2.3 | DNS, outbound TCP, HTTP POST | Sandbox network is a non-internal bridge, and the runner adds no egress rule | NOT MEASURED |
| 2.4 | 169.254.169.254 | Nothing in code or compose blocks it | NOT MEASURED |
| 2.5 | Env secrets | Runner passes only PORT, HOST, ASPNETCORE_URLS, SERVER_PORT, FLASK_RUN_*, CI, PYTHONUNBUFFERED (`sandbox_runner.py:290-296`) | NOT MEASURED |
| 2.6 | Write outside workspace | ReadonlyRootfs requested; `/tmp`, `/root/.cache` and `/home/node/.npm` are writable tmpfs | NOT MEASURED |
| 2.7 | Fork bomb | PidsLimit 64 requested | NOT MEASURED |
| 2.8 | Memory balloon | Memory 256 MiB requested; no MemorySwap set | NOT MEASURED |
| 2.9 | Infinite loop | See 2.12 | NOT MEASURED |
| 2.10 | Docker socket | Not mounted into the sandbox (the only bind is the workspace volume) | NOT MEASURED |
| 2.11 | Neighbour services | Compose has **no database or cache service**. The `api` container (aliases `api`, `preflight_api`) is on the sandbox network and serves `/runner`, which holds the Docker socket | NOT MEASURED |

**2.12 Watchdog.** It exists: a daemon thread in `sandbox_runner.py:368-389`, polling every 3 s, reaping sessions older than `SANDBOX_MAX_LIFETIME` (compose: 120 s).
- Measured against a **fake container object** (not a real container) with `SANDBOX_MAX_LIFETIME=5`: `container.remove` and `volume.remove` were called at 6.01 s, 6.01 s and 6.01 s in 3 runs.
- Real-container kill time at 120 s: NOT MEASURED.
- Defect found in the same run: after a reap, `status()` returns `{'repo_name': 'fake', 'status': 'stopped', 'logs': [], 'pid': None, 'exit_code': None}`, with no `stopped_reason` and the logs gone. The "page never settles" confirmation probe (`preflight.py:1688-1717`) looks for `"watchdog"` in `stopped_reason`, so that path can never fire.

**2.13 Cleanup after scan.**
- Real `docker ps -a` / `docker volume ls`: NOT MEASURED.
- Measured with the fake object: the watchdog path calls `remove(force=True)` on both the container and the volume.
- Code reading: there is no startup sweep by the `preflight.sandbox=true` label. Containers started before an API restart lose their in-memory session and are never reaped.

**2.14 Control test.** NOT MEASURED. Without it, a future probe failure cannot be attributed to containment. How to run it:
- Through the scan path, relax what is env-driven: `SANDBOX_DOCKER_NETWORK=bridge SANDBOX_PIDS_LIMIT=4096 SANDBOX_MEMORY_LIMIT=2g`.
- `read_only`, `cap_drop`, `security_opt`, `user` and `tmpfs` are hard-coded (`sandbox_runner.py:283-311`). Relaxing them needs a temporary code change or a hand-rolled `docker run` without those flags.

---

## Group 3: scoring formula, tested directly

`compute_preflight_scoring()` called with synthetic lists. Script output is quoted. 0 network attempts.

**3.1 Arithmetic.** Input: RED-01, RED-04, ORANGE-07, ORANGE-08, ORANGE-09, YELLOW-11, all confirmed.
```
sec=60 rel=35 cor=63 main=96 readiness=58 verdict=not_ready
hand-computed round(60*.35+35*.30+63*.20+96*.15)=58  MATCH=True
weights reported: security 0.35, reliability 0.3, correctness 0.2, maintainability 0.15
```
Penalties per registry ID are correct (red 40, ORANGE-07/08 25, ORANGE-09/10 12, yellow 4). Floor works: 3x RED-01 gives security 0, readiness 65.
Single findings: RED-01 86, RED-04 88, ORANGE-07 92, ORANGE-08 95, ORANGE-09 98, YELLOW-12 99.

**3.2 Unconfirmed deduct nothing: confirmed.** All six with `unconfirmed=True` give 100/ready. `unconfirmed="true"` (string) gives the same.
Note: a finding with **no `unconfirmed` key deducts in full** (RED-01 with no flag gives 86). The default is "confirmed".

**3.3 Before/after shape.**
```
BEFORE (RED-03 confirmed):        readiness=86  verdict=not_ready
AFTER  (same finding, fixed=True): readiness=100 verdict=ready
AFTER  (finding removed):          readiness=100 verdict=ready
```
One confirmed security red moves the score 86 to 100. A reliability red moves it 88 to 100.

**3.4 Verdict thresholds.**
- **The suspected branch `readiness >= 100 or (orange_count == 0 ...)` does not exist** anywhere in the repo (grep over .py, .js, .mjs and .html returned nothing). The current logic (`preflight.py:316-334`): red or score below 70 is not_ready; else orange or score below 90 is launch_with_care; else ready.
- A score of 100 with an orange present cannot occur with the registry. The smallest orange penalty is 12 on Correctness, so the maximum is 98, and that returns launch_with_care. An unknown-ID orange (tier only) gives 95, also launch_with_care. **The suspected bug is not present.**
- Other results: 2 yellows 99 ready; 12 yellows 93 ready; 2x ORANGE-08 90 launch_with_care; 3x ORANGE-07 78 launch_with_care.
- **Text bug:** 25 yellows (maintainability 0) give 85, launch_with_care, with message `"Nothing dangerous, but 0 UX/operational hazard detected. Users will hit problems."`

**3.5 Substring routing: confirmed defective.** Matching is `canonical_name in rule or rule in canonical_name`, first registry match wins (`preflight.py:257-263`). Then keyword fallbacks (`preflight.py:265-277`).

| Input | Routed to | Correct? |
|---|---|---|
| `rule="Auth bypass: search and sort are wrong place to check admin", tier=red` | **correctness 12, counted as orange**, verdict launch_with_care | **No.** A red security finding is downgraded |
| `rule="search", tier=red` | correctness 12, orange | **No** |
| `rule="Search endpoint leaks other users' records"` (no tier, no id) | **nothing deducted** | **No.** Silently dropped |
| `rule="SQL injection in the search endpoint", tier=red` | security 40 | Yes (via keyword "inject") |
| `rule=""` (unknown id, or no id and no tier) | **security 40 as RED-01 Leaked API keys** | **No** |
| `rule="a"` | **security 40 as RED-01** (since "a" is in "leaked api keys") | **No** |
| `rule="Passwords stored in plaintext", tier=red` | **reliability 40** | **No.** Not in keyword list |
| `rule="[P0] Token leaked while loading profile"` | **reliability 40** | **No** |
| `rule="injection", tier=orange` | security 40, red | Promoted from orange to red |

The custom_code path builds rules `"RED Vulnerability"`, `"ORANGE Hazard"` and `"YELLOW Polish"` (`preflight.py:1789-1794`). Those route only by tier, and the area falls to keyword fallbacks.

**3.6 Registry.** 13 defect types (6 red, 4 orange, 3 yellow). `defects/registry.json` and `backend/app/defects/registry.json` are byte-identical. Every ID routes correctly by `id`, by lowercase id, by `defect_id`, and by canonical name. Every rule string in `RED_RULES`, `ORANGE_RULES` and `YELLOW_RULES` routes correctly. **IDs the function fails to route: none.** The failures are in free-text findings without an ID (3.5).

---

## Group 4: what the test suite proves

**4.1 Full suite** (`pytest backend/tests`, network guard on, 0 outbound attempts):

| Run | Total | Passed | Failed | Skipped |
|---|---|---|---|---|
| From `backend/` (as shipped) | 167 | 145 | 22 | 0 |
| From repo root, `pyyaml` added to the venv | 167 | 148 | 19 | 0 |

- The 3 that differ are artefacts. Two `test_checklist_verification` tests read `defects/registry.json` and `.env.example` by relative path, so they pass only from the repo root. One test imports `yaml`, which is **not in `requirements.txt`**.
- The 19 remaining failures are all in `test_auth.py`. They are real: `require_user` (session + CSRF + ownership) is used by 0 routes in `main.py`, and every protected route uses `verify_api_key`, which checks neither CSRF nor ownership. Examples, verbatim:
  - `test_llm_api_key_is_masked`: `'supersecret' unexpectedly found in 'sk-supersecretvalue1234'`. `GET /api/v1/config/llm` returns `get_llm_config()` unmasked (`main.py:1181-1183`).
  - `test_csrf_token_required_for_state_changes`: `200 != 403`
  - `test_listing_only_shows_own_repositories`: `['unowned', 'b-one', 'a-one', 'a-two'] != ['a-one', 'a-two']`
  - `test_cross_user_access_is_denied_everywhere`: `404 != 200`
  - `test_mcp_execute_policy`: `200 != 403`

**4.2 TESTING=1 bypasses.** I measured the effect by rerunning the suite with `TESTING=0 MOCK_LLM=0` forced before every test. **4 tests pass only because of the bypass:**

| Test | Failure with bypass off | Bypass responsible |
|---|---|---|
| `test_preflight.py::test_preflight_endpoint` | `429 != 200` | scan rate limiter (`main.py:192`) |
| `test_sandbox.py::test_preflight_code_review_and_download` | `429 != 200` | scan rate limiter |
| `test_preflight.py::test_apply_preflight_bug_fix_and_endpoint` | `422 != 200` | fix path (`preflight.py:2278`) appends a canned comment in place of the real flow |
| `test_sandbox.py::test_sandbox_lifecycle_execution` | `503: Isolated container runner required (SANDBOX_RUNNER_URL is unset)...` | `sandbox.py:1100`. **TESTING=1 enables unsafe host-process execution** |

With the bypass off, 3 tests (`test_discover_project_launch_plan_node`, `..._python_fastapi`, `test_sandbox_lifecycle_execution`) **attempted DNS for `api.openai.com`**. The guard blocked it. The cause is state left by `test_llm.py`, which calls `set_llm_config("https://api.openai.com/v1", ...)` into module memory.

Every bypass in app code:

| Location | Disables |
|---|---|
| `core/llm.py:265` (also MOCK_LLM) | `verify_llm_connection` returns `(True, "Connected (Test Mode)")` |
| `core/llm.py:429` (also MOCK_LLM) | `call_llm` returns a canned clean JSON verdict |
| `core/llm.py:463` | minimum 45 s LLM timeout |
| `core/llm.py:505` | live Vultr model catalog fetch |
| `core/preflight.py:1402` | no-key check, so the canned `call_llm` runs and **the engine is labelled `"vultr-inference"`** with no model contacted |
| `core/preflight.py:2278` (unless REAL_LLM) | real fix generation; appends a marker comment |
| `core/sandbox.py:35` (unless REAL_RUNNER) | all runner HTTP calls (forced ConnectionError) |
| `core/sandbox.py:525` | LLM launch-plan discovery |
| `core/sandbox.py:1100` | **the Blast Radius Zero gate**, allowing host-process execution |
| `core/sandbox.py:1733`, `:1884` (unless REAL_LLM) | LLM code review and crash diagnosis |
| `core/supabase_client.py:54`, `:104` | Supabase connectivity and upload (fake success) |
| `core/graph.py:236` | Neo4j |
| `main.py:113` | graph schema init on startup |
| `main.py:192` | per-IP concurrency and rate limit |
| `main.py:946` | LLM-connected precondition on an endpoint |

**4.3 Real vs mock split** (167 collected items; static classification of each test body, its class `setUp` and its decorators; `tests/__init__.py` replaces `neo4j` with a MagicMock for every test):

| What the test exercises | Count |
|---|---|
| Real container | **0** |
| Real outbound HTTP call | **0** (guard logged 0 attempts in the normal run) |
| In-process FastAPI TestClient, no mocks (real SQLite and temp files) | 33 |
| In-process TestClient plus `patch`/`MagicMock` | 33 |
| Mock only, no TestClient | 19 |
| Real file or temp dir, no mocks | 5 |
| Pure function or literal assertions, no mocks | 77 |

52 tests use a mock somewhere, 115 do not. "No mock" here still means in-process and under `TESTING=1` for most of them.

**4.4 Regression test (fails on defective code, passes after patch): none exists.**
- `test_apply_preflight_bug_fix_and_endpoint` mocks `call_llm` to return a string containing `Preflight Auto-Fix Applied`. It then asserts that string is in the file. It never re-runs detection on the patched file.
- `test_fix_validation_rejects_truncation_and_unchanged_snippet` checks the patch validator, not defect removal.
- I did not run a both-directions demonstration because no such test exists.

---

## Group 5: Vultr compliance (static plus offline execution)

**5.1 Non-Vultr inference hostnames and local ports** (grep over the repo, excluding `frontend/vendor`). No hits for `generativelanguage`, `anthropic`, `together`, `mistral.ai`.

| File:line | Value | Kind |
|---|---|---|
| `backend/app/core/embedder.py:38` | `EMBEDDING_API_URL` default `https://api.openai.com/v1` | **Default, live.** Compose sets neither `EMBEDDING_API_URL` nor `VLLM_BASE_URL`. Measured below |
| `backend/app/core/embedder.py:18` | `https://api.openai.com/v1` when the URL is empty | Fallback |
| `backend/app/core/embedder.py:42-44, 59-63` | key = `EMBEDDING_API_KEY or OPENAI_API_KEY or LLM_API_KEY` | Fallback that sends the Vultr key to the embedding host |
| `backend/app/main.py:1194-1195` | `https://api.openai.com/v1`, `gpt-4o-mini` | Fallback in `POST /api/v1/config/llm` when payload and current config are both empty (for example `LLM_BASE_URL=""`) |
| `backend/app/main.py:1269-1270` | `api.openai.com` in Field descriptions | Dead (documentation string) |
| `backend/app/core/llm.py:271` | model `gpt-4o-mini` | Fallback, effectively dead (config model defaults to `deepseek-v4.1-flash-1017`) |
| `backend/app/core/llm.py:428` | docstring naming OpenAI, OpenRouter, Groq, Ollama | Dead |
| `backend/app/core/llm.py:72-75, 105-109` | base_url replaced by `http_proxy` when it contains `20128` | **Rewrite**, see 5.2 |
| `backend/app/core/llm.py:140-146` | `localhost` / `127.0.0.1` rewritten to `host.docker.internal` inside Docker | Rewrite of local targets |
| `backend/app/core/sandbox.py:783` | `http://host.docker.internal:10808` proxy | Used only in the host-process .NET install path |
| `backend/app/core/sandbox.py:67` | `https://api.e2b.dev` | Default (sandbox provider, not inference) |
| `frontend/index.html:1777-1786` | preset buttons: `api.openai.com`, `openrouter.ai`, `api.groq.com`, `host.docker.internal:11434` | User-selectable presets, live in the UI |
| `frontend/index.html:3574-3575` | `api.openai.com/v1`, `gpt-4o-mini` | UI fallback when the server config is empty |
| `frontend/index.html:963, 1005, 1007` | `gpt-4o-mini` label; `:10808` proxy placeholder | Display only |
| `README.md:271-272` | documents `LLM_BASE_URL` default `api.openai.com`, `LLM_MODEL` default `gpt-4o-mini` | **Docs contradict code** (code default is Vultr) |
| `USER_GUIDE.md:118-123` | OpenAI, OpenRouter, Ollama setup instructions | Docs |
| `backend/tests/test_llm.py:49, 65` | `set_llm_config("https://api.openai.com/v1", ...)` | Test; leaks into later tests (4.2) |
| `backend/app/core/celery.py:7-8`, `graph.py:161-249` | `redis://localhost:6379`, `bolt://localhost:7687` | Not inference. Note: `get_llm_config()` reads Redis **before** env (`llm.py:54-81`), so a stored config overrides `LLM_BASE_URL` |

Measured (compose LLM env with a dummy key, request intercepted before send):
```
EMBEDDING_API_URL resolved to: https://api.openai.com/v1
[('https://api.openai.com/v1/embeddings', 'Bearer DUMMY-VULTR-KEY-FOR-MEASUREMENT')]
```
`get_embedding` is called from `core/rag.py:139` (query and chat). `verify_vllm_status()` reported `'status': 'connected'` for this host while all network was blocked. That status is not a probe.

**5.2 Proxy-port rewrite to `https://api.openai.com/v1`: not present in this version.** `llm.py` contains 0 occurrences of `api.openai.com`. The proxy-port logic, `KNOWN_PROXY_PORTS = {10808, 10809, 7890, 7891}` (`llm.py:26`), does this:
- `llm.py:62-65` and `:89-92`: log a warning, no rewrite.
- `llm.py:112-119` (`set_llm_config`): raises `ValueError`. Measured: `http://127.0.0.1:7890` raised. **`http://127.0.0.1:7890/v1` was accepted**, because the check is skipped when the URL ends in `/v1`.
- `llm.py:277-283` (`verify_llm_connection`): returns False with a message pointing to Vultr.

The rewrite that does exist is to a local gateway. In `set_llm_config` (`llm.py:106-109`) and `get_llm_config` (`llm.py:72-75`), any `http_proxy` containing `20128` replaces `base_url`. Measured:
```
set_llm_config(base='https://api.vultrinference.com/v1', http_proxy='http://host.docker.internal:20128')
get_llm_config() -> base_url 'http://host.docker.internal:20128/v1', http_proxy ''
```
Trigger: typing a port-20128 address into the dashboard's HTTP Proxy field. The Vultr endpoint is then silently replaced.

**5.3 Scan with no API key: completes.** `POST /api/v1/review/preflight-report` via TestClient, `TESTING` unset, no keys, network guard on, a 2-file synthetic repo (hardcoded `sk-proj-...` key, string-concatenated SQL, `console.log`, a "Pay now" button, empty README):
```
HTTP 200  elapsed_s 0.02
evaluations.engine = 'heuristic-fallback'   (also red_defects.engine, orange_defects.engine, yellow_defects.engine)
readiness 100 ready counts {'red': 0, 'orange': 0, 'yellow': 0, 'total': 4}
confirmed: []
uncertain: Leaked API keys (src/api.js:1), Database injection (src/api.js:4), Debug code x2
verdict: "🟢 Ready to launch | Nothing serious found. Your app passed all preflight safety and reliability checks. Go."
markdown: "> **Evaluation Engine:** Heuristic Fallback (Local Static Rules - No LLM Key)"
```
- The fallback **is marked in a machine-readable field** (`evaluations.engine` and per tier), as well as in the markdown.
- With `TESTING=1` or `MOCK_LLM=1`, the same field reads `"vultr-inference"` with no model contacted (`preflight.py:1402-1407`).
- **The verdict is wrong for this input.** Both red findings were detected, then marked unconfirmed by `confirm_candidates_in_sandbox`. The confirmation probes are mostly static: probe 1.1 regex-scans `*.html` files and, failing that, **the candidate's own `snippet`** (`preflight.py:1600-1615`). It did not flag the "Pay now" button. The heuristic path sets `unconfirmed: False` directly only for over-complex files (`preflight.py:993`).

**5.4 `eval/run.mjs`: NOT RUN.** Blocked because Node.js is not installed.
- Static reading: it makes **no network call at all**. `API_BASE` is printed but never fetched.
- It reimplements the scoring formula in JavaScript and does not call `compute_preflight_scoring`.
- It runs 4 hard-coded synthetic cases. There is no mock or offline mode because it is entirely offline already.
- The Python function gives the numbers those cases expect (measured in Group 3: clean 100/ready; RED-01 86/not_ready; ORANGE-07 92/launch_with_care; unconfirmed 100/ready).
- **Consequence for the deferred work: `run.mjs` cannot measure arbiter false positives or recall in its current form.** Spending credits on it would test nothing model-related. A labelled-case harness has to be written first.

---

## DEFERRED (need model calls, not run)

- Arbiter false-positive rate and recall against a live model. Precondition: a harness that calls the real scan endpoint on labelled repos. `eval/run.mjs` does not do this (5.4).
- End-to-end scan timing and token cost with a live model.
- Runtime verification that every inference call reached `api.vultrinference.com`. Include the embedding path (5.1 shows it targets `api.openai.com`) and the Redis-stored config override.

---

## Group 6: honest limits

**6.1 What this run does not cover**
- Any real container: Groups 1 (APPLIED column) and 2 entirely.
- The Docker daemon's own handling of the requested HostConfig (for example the cgroup v1/v2 differences in how `Memory` and `NanoCpus` take effect).
- The frontend, the MCP server (`mcp_server.py`), Celery and Neo4j paths, the E2B driver, and the Coolify deployment.
- Real LLM behaviour, including whether the three LLM passes produce IDs or free text. That decides how much 3.5 matters in practice.
- `eval/run.mjs` execution.

**6.2 Isolation from Docker, not a hardware VM.** Every requested control is a Linux namespace, cgroup or LSM setting on the **shared host kernel**: ReadonlyRootfs, CapDrop, no-new-privileges, PidsLimit, Memory, NanoCpus, the bridge network and the non-root UID. A kernel exploit from inside the sandbox reaches the host. No seccomp profile beyond Docker's default is requested. No user-namespace remapping, gVisor or Kata runtime is configured. `README.md:42` lists "E2B / OpenSandbox MicroVMs", but the compose deployment uses the Docker runner. The E2B driver is selected only when `E2B_API_KEY` is set and was not exercised. The runner holds the host Docker socket inside the public API process (finding 3), which is effectively root on the host if that process is compromised.

**6.3 Could not test, and why**
- `docker inspect`, all eleven probes, the real watchdog kill time, container and volume removal, and the control test: no Docker engine on this machine.
- Whether the non-root `appuser` (UID 1000) in the API image can open the mounted `/var/run/docker.sock`: depends on the host socket's group, no Docker.
- `eval/run.mjs`: no Node.js.
- The README's "Traefik labels" claim for `docker-compose.coolify.yml`: checked statically, and the file contains no `labels` key. Not tested in a Coolify deploy.

---

## 7. Network attempts caught by the guard (all blocked, none sent)

| Run | Attempts |
|---|---|
| Group 3 scoring | none |
| Full test suite (both runs) | none |
| Test suite with TESTING forced off | `api.openai.com` DNS x3 (test-state leak, 4.2) |
| Group 1 request capture | none |
| 5.1 embedding trace | `api.openai.com` DNS x2; `127.0.0.1:6379` (Redis) |
| 5.3 no-key scan | `127.0.0.1:6379` / `::1:6379` (Redis) only |

---

## Summary table

| ID | Measurement | Result |
|---|---|---|
| 1.1 | ReadonlyRootfs | Claimed: not claimed. Requested: true. Applied: NOT MEASURED (no Docker engine) |
| 1.2 | Memory | Claimed 256m, README 1 GB. Requested 268435456 B = 256 MiB. Applied: NOT MEASURED (no Docker). **README MISMATCH** |
| 1.3 | NanoCpus | Claimed 0.5 (separate env var), README 1.0. Requested 500000000 = 0.5. Applied: NOT MEASURED (no Docker). **MISMATCH:** claim does not follow request (2.0 requested, 0.5 claimed) |
| 1.4 | PidsLimit | Claimed 64, README 100. Requested 64. Applied: NOT MEASURED (no Docker). **README MISMATCH** |
| 1.5 | CapDrop | ALL / ALL (both literals). Applied: NOT MEASURED (no Docker) |
| 1.6 | SecurityOpt | no-new-privileges:true both. Applied: NOT MEASURED (no Docker) |
| 1.7 | NetworkMode | Claimed "isolated_bridge". Requested `preflight_sandbox_net`, a plain non-internal bridge shared with `api`. Applied: NOT MEASURED (no Docker). **MISMATCH** |
| 1.8 | User | Not claimed. Requested 1000:1000 (non-root). Applied: NOT MEASURED (no Docker) |
| 1.9 | Tmpfs | Not claimed. Requested /tmp (noexec,nosuid,128m), /root/.cache 64m, /home/node/.npm 64m. Applied: NOT MEASURED (no Docker) |
| 2.1-2.11 | Containment probes | NOT MEASURED, contained n/11 unknown (no Docker engine). Script written: `eval/containment_probe.py` |
| 2.12 | Watchdog | Exists. Fake-container reap at 6.01 s for 5 s lifetime (3/3). Real kill time NOT MEASURED (no Docker). Reap erases `stopped_reason` and logs |
| 2.13 | Container and volume removal | Fake object: both removed. Real: NOT MEASURED (no Docker). No orphan sweep after restart |
| 2.14 | Control test | NOT MEASURED (no Docker) |
| 3.1 | Weights and penalties | Correct: 58 computed = 58 expected; 35/30/20/15; 40/25/12/4 |
| 3.2 | Unconfirmed deduct 0 | Confirmed (bool and string). Missing flag means confirmed |
| 3.3 | Before/after | RED-03: 86 not_ready to 100 ready |
| 3.4 | Verdict thresholds | Suspected `>= 100` bug: branch does not exist; orange max score 98, never "ready". Text bug "0 UX/operational hazard" |
| 3.5 | Substring routing | **Defective:** "search..." auth bypass becomes orange correctness 12; empty or "a" rule becomes RED-01 security 40; plaintext passwords become reliability |
| 3.6 | Registry routing | 13 types (6/4/3); 0 IDs fail to route |
| 4.1 | Test suite | 167 total: 145 pass / 22 fail / 0 skip as shipped; 148 / 19 / 0 from repo root with pyyaml. 19 real auth failures |
| 4.2 | TESTING bypass | 15 bypass sites; 4 tests pass only because of them; one bypass enables host-process execution |
| 4.3 | Real vs mock | 0 real container, 0 real HTTP; 66 in-process TestClient (33 with mocks); 19 mock-only; 5 real file; 77 pure/literal |
| 4.4 | Regression test | None exists |
| 5.1 | Non-Vultr hosts | Live default: embeddings go to api.openai.com with the Vultr key (measured). UI presets: OpenAI, OpenRouter, Groq, Ollama. README documents an OpenAI default |
| 5.2 | Proxy rewrite to api.openai.com | Not present (0 occurrences in llm.py). Port-20128 proxy rewrites base_url to `host.docker.internal:20128/v1` (measured). `/v1` suffix bypasses the proxy-port guard (measured) |
| 5.3 | No-key scan | Completes, HTTP 200 in 0.02 s; `engine: "heuristic-fallback"` machine-readable; verdict "Ready... Go." despite a detected leaked key and SQL injection |
| 5.4 | run.mjs offline check | NOT RUN (no Node.js). Static: fully offline, never calls the API, cannot measure arbiter quality |

---

## Addendum, same day: run on OrbStack (Docker Engine 29.8.2, linux/arm64)

Stack started with `docker compose up -d --build`, a random `SANDBOX_RUNNER_TOKEN`, and no LLM key. No model call was made.

**The shipped stack cannot create any sandbox container.** Measured inside `preflight_api`:
```
uid=1000(appuser) gid=1000(appgroup) groups=1000(appgroup)
srw-rw---- 1 0 0 0 Nov  5 08:14 /var/run/docker.sock
ERR: DockerException("Error while fetching server API version: ('Connection aborted.', PermissionError(13, 'Permission denied'))")
```
`/runner/health` returns `{"status":"degraded","detail":"Docker daemon not reachable"}`, and `/runner/sessions/start` returns `{"detail": "Docker daemon unavailable on this host."}`. Group 1 APPLIED therefore stays **NOT MEASURED: no sandbox container exists on the shipped configuration.** On a standard Linux host the socket is `root:docker 0660`, and `appuser` is not in that group, so the same failure is expected there. That is not measured.

**When the runner fails, untrusted code runs inside the API container.** `start_sandbox_session` (`sandbox.py:1081-1103`) logs "Activating host-process fallback". The Blast Radius Zero guard only fires when `SANDBOX_RUNNER_URL` is unset, and compose sets it. Measured via `POST /api/v1/sandbox/start` with a local test user: HTTP 200, `"status": "running"`, PID 1364 inside `preflight_api`, working directory `/app/repos/containment_probe`. None of the requested containment flags apply on this path.

Network: `docker network inspect preflight_sandbox_net` gives `internal=false driver=bridge containers=preflight_api`.

### Group 2 results (host-process fallback in the API container, verbatim)

| # | Probe | Verbatim result | Contained? |
|---|---|---|---|
| 2.1 | DNS | `["2606:4700:10::6814:179a", 443, 0, 0]` | **No** |
| 2.2 | Outbound TCP | `connected to 1.1.1.1:443` | **No** |
| 2.3 | HTTP POST | `URLError(OSError(101, 'Network is unreachable'))` | Not attributable: DNS returned IPv6 first and this VM has no IPv6 route. TCP over IPv4 succeeded (2.2) |
| 2.4 | 169.254.169.254 | `URLError(TimeoutError('timed out'))` | Not attributable: OrbStack has no metadata service. Unproven on Vultr |
| 2.5 | Env secrets | `variable names only: []` | Yes, by the env allow-list (`sandbox.py:898`) |
| 2.6 | Write outside workspace | `/`, `/etc`, `/usr`, `/home`: `PermissionError(13, 'Permission denied')`; `/tmp/probe.txt: WRITE OK` | Partial, by Unix permissions as uid 1000, not a read-only rootfs. The repo dir itself (`/app/repos`, bind-mounted from your machine) was not tested |
| 2.7 | Fork bomb | `children started=500; first error=None` | **No** (no PIDs limit) |
| 2.8 | Memory balloon | `allocated 768 MB without being killed` | **No** (no memory limit) |
| 2.9 / 2.12 | Infinite loop, watchdog | heartbeats to `115.0` s, `status: running, uptime 130` at 130 s; stopped manually | **No watchdog on this path** |
| 2.10 | Docker socket | `PermissionError(13, 'Permission denied')` | Yes, by uid 1000 lacking socket access (the same fact that breaks the runner) |
| 2.11 | Neighbours | `api:8000: CONNECTED`, `frontend:80: CONNECTED`, `host.docker.internal:8000: CONNECTED`; `/runner/health: HTTP 401` | **No** (reaches the API, which holds the runner; the token is the only barrier). Compose has no redis, neo4j or postgres |

**Contained: 2 of 11 clearly (2.5, 2.10), 1 partial (2.6), 2 not attributable (2.3, 2.4), 6 not contained.**

**2.13** Container and volume removal: not applicable, since no container was created. The fallback process exited on `/api/v1/sandbox/stop`.

**2.14** Control test: NOT RUN (usage limit reached). The run above already shows the uncontained baseline for 2.1, 2.2, 2.7, 2.8, 2.9 and 2.11.

Still to do: a run where the runner can reach the socket (for example `user: root` or `group_add` with the socket's GID on the `api` service, as a measurement-only override) to fill in Group 1 APPLIED and rerun Group 2 inside a real sandbox container, plus the control run.

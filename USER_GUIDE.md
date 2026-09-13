# راهنمای جامع کاربری و آموزش سامانه CodeGraph Brain Enterprise
### (Universal Code Knowledge Graph, Polyglot GraphRAG & AI Architectural Review Platform)

---

## فهرست مطالب
1. [معرفی سامانه و معماری سازمانی](#1-معرفی-سامانه-و-معماری-سازمانی)
2. [پیش‌نیازها و راه‌اندازی با Docker Compose](#2-پیش‌نیازها-و-راه‌اندازی-با-docker-compose)
3. [آموزش بخش‌های چهارگانه داشبورد مدیریتی](#3-آموزش-بخش‌های-چهارگانه-داشبورد-مدیریتی)
4. [تحلیل معماری و کشف بوی کد (Code Smells)](#4-تحلیل-معماری-و-کشف-بوی-کد-code-smells)
5. [بررسی خودکار پول ریکوئست و شعاع تخریب (PR Diff Review)](#5-بررسی-خودکار-پول-ریکوئست-و-شعاع-تخریب-pr-diff-review)
6. [مدیریت چند ریپازیتوری (Multi-Repository Hub)](#6-مدیریت-چند-ریپازیتوری-multi-repository-hub)
7. [راه‌اندازی وب‌هوک گیت‌هاب جهت ایندکس خودکار (CI/CD)](#7-راه‌اندازی-وب‌هوک-گیت‌هاب-جهت-ایندکس-خودکار-cicd)
8. [آموزش اتصال ایجنت‌های هوش مصنوعی با پروتکل MCP](#8-آموزش-اتصال-ایجنت‌های-هوش-مصنوعی-با-پروتکل-mcp)
9. [مستندات REST API و اندپوینت‌های کلیدی](#9-مستندات-rest-api-و-اندپوینت‌های-کلیدی)
10. [کوئری‌نویسی پیشرفته در دیتابیس Neo4j](#10-کوئری‌نویسی-پیشرفته-در-دیتابیس-neo4j)
11. [نکات امنیتی و عیب‌یابی (Troubleshooting)](#11-نکات-امنیتی-و-عیب‌یابی-troubleshooting)

---

## ۱. معرفی سامانه و معماری سازمانی

سامانه **CodeGraph Brain Enterprise** یک پلتفرم جامع جهت مهندسی معکوس ساختار کدهای سازمانی و تبدیل آن‌ها به **گراف دانش رابطه‌ای و معنایی** است.

### قابلیت‌های اصلی سامانه:
- **موتور چندزبانه (Universal Polyglot Engine):** پارس عمیق زبان‌های Python, JavaScript, TypeScript, Go, Java, C, C++, C#, Rust به همراه پشتیبانی از زبان‌های دیگر (Ruby, PHP, Kotlin, Swift, Scala, SQL, Shell) از طریق الگوریتم فال‌بک عمومی.
- **گراف دانش ترکیبی (Hybrid Neo4j Graph + Vector):** ادغام ساختار درختی و ارتباطات فراخوانی توابع با بردارهای ۱۵۳۶ بعدی معنایی در یک پایگاه داده مشترک.
- **موتور تحلیل سلامت معماری (Architecture Health):** کشف چرخه‌های فراخوانی دوری (Circular Dependencies)، شناسایی توابع حجیم و گلوگاه (God Functions)، و شناسایی کدهای بدون استفاده (Dead Code).
- **ارزیابی شعاع تخریب (Cascading Blast Radius):** محاسبه زنجیره شکست ناشی از تغییر هر تابع تا ۳ لایه عمق.
- **بازبینی معماری PR (Architectural PR Review):** تولید خودکار گزارش ریسک شکست و ارائه چک‌لیست تست رگرسیون با تلفیق گراف و مدل زبانی.
- **سرور جامع FastMCP با ۱۰ ابزار کاربردی:** جهت اتصال به ایجنت‌های هوش مصنوعی نظیر Claude Desktop, Cursor و Antigravity.

---

## ۲. پیش‌نیازها و راه‌اندازی با Docker Compose

### نیازمندی‌ها:
- **Docker** نسخه 20.10 به بالا
- **Docker Compose** نسخه 2.0 به بالا
- حداقل ۴ گیگابایت حافظه رم آزاد

### گام‌های راه‌اندازی:
```bash
# ۱. رفتن به پوشه پروژه
cd code-graph-brain

# ۲. ایجاد فایل تنظیمات محیطی از روی نمونه
cp .env.example .env

# ۳. ویرایش مقادیر دلخواه در .env
# nano .env

# ۴. اجرای کامل سرویس‌ها به صورت پس‌زمینه
docker compose up -d --build
```

#### تنظیم میرورهای شبکه داخلی (IME Corporate Mirrors):
اگر در شبکه سازمانی هستید که اینترنت مستقیم ندارد، فایل `.env` را به شکل زیر تنظیم نمایید:
```env
DOCKER_REGISTRY=dockerhub.ime.co.ir/
PYPI_MIRROR=https://pypi.repo.ime.co.ir/repository/pypi-group/simple/
PYPI_HOST=pypi.repo.ime.co.ir
DEBIAN_MIRROR=https://linux.repo.ime.co.ir/repository/debian
WEB_PORT=8001
MCP_PORT=8002
```
همچنین می‌توانید در تنظیمات Docker Desktop (بخش **Docker Engine**) یا فایل `/etc/docker/daemon.json` میرور داکر را تعریف نمایید:
```json
{
  "registry-mirrors": ["https://dockerhub.ime.co.ir"],
  "insecure-registries": ["dockerhub.ime.co.ir"]
}
```

### وضعیت سرویس‌های فعال (دو پورت استاندارد شبکه):
- **داشبورد گرافیکی وب و مدیریت مخازن:** [http://localhost:8001](http://localhost:8001)
- **مستندات تعاملی Swagger API:** [http://localhost:8001/docs](http://localhost:8001/docs)
- **پایش سلامت سرور (Healthcheck):** [http://localhost:8001/healthz](http://localhost:8001/healthz)
- **سرور پروتکل MCP (حالت SSE برای ایجنت‌ها):** [http://localhost:8002/sse](http://localhost:8002/sse)
- **پنل مدیریت دیتابیس Neo4j Browser:** [http://localhost:7474](http://localhost:7474) (داخلی سرور)

---

## ۳. آموزش بخش‌های چهارگانه داشبورد مدیریتی

داشبورد سازمانی سامانه در آدرس `http://localhost:8001` قرار دارد و شامل ۴ بخش اختصاصی است:

### ۱. نمای گراف و چت معنایی (Graph & RAG):
- **بوم تعاملی Cytoscape:** بزرگ‌نمایی و چرخش کل ساختار کد پروژه‌ها.
- **فیلتر مخازن (Scope Filter):** امکان تمرکز روی یک مخزن خاص یا مشاهده کلیه پروژه‌های سازمان.
- **Node Inspector:** کلیک روی هر تابع یا کلاس برای مشاهده آنی مسیر فایل، داک‌استرینگ، و متن سورس‌کد با قابلیت کپی.
- **GraphRAG Search:** پرسش به زبان طبیعی از کدبیس با دریافت پاسخ ترکیبی هوش مصنوعی و ارجاع دقیق به خطوط سورس‌کد.

### ۲. نمای سلامت معماری (Architecture Health):
- بررسی چرخه‌های وابستگی و حلقه‌های صدا زدن.
- نمایش توابع گلوگاه با ضریب ریسک بالا (God Functions).
- شناسایی کدهای ایزوله و بدون استفاده (Dead Code Candidates).

### ۳. هاب مخازن (Repositories):
- نمایش کارت‌های پروژه‌های ایندکس‌شده با شمارش فایل‌ها، کلاس‌ها، توابع و برچسب زبان‌ها.
- امکان بازبینی، تمرکز روی گراف، یا حذف کامل و آبشاری ریپازیتوری از دیتابیس و دیسک.

### ۴. بررسی دیف و شعاع تخریب (Diff Impact & PR Review):
- تحلیل اثرگذاری یک متد بر سایر بخش‌ها قبل از دستکاری کد.
- پیست کردن خروجی `git diff` و دریافت تحلیل ساختاری ریسک همراه با راهنمای آزمون کیفیت (QA).

---

## ۴. تحلیل معماری و کشف بوی کد (Code Smells)

کدهای درهم‌پیچیده باعث افزایش باگ‌های ناخواسته می‌شوند. سامانه با کوئری‌های عمیق بر روی گراف، این موارد را کشف می‌کند:

- **چرخه‌های دوری (Circular Dependencies):** زنجیره‌هایی نظیر `A -> B -> C -> A`. این ساختارها نگهداری پروژه را به شدت دشوار کرده و باعث وابستگی متقابل ماژول‌ها می‌شوند.
- **توابع با ریسک بالا (High-Risk Hubs):** متدهایی که تعداد بسیار زیادی تابع دیگر به آن‌ها وابسته‌اند (High In-Degree) یا خود تعداد نامتعارفی تابع را فرامی‌خوانند (High Out-Degree). هرگونه تغییر در این متدها مستلزم دقت بسیار بالاست.
- **کدهای مرده (Dead Code):** توابعی که نه در کلاس‌ها و نه توسط توابع دیگر صدا زده نمی‌شوند و کاندید پاک‌سازی یا انتقال به کتابخانه‌های خارجی هستند.

---

## ۵. بررسی خودکار پول ریکوئست و شعاع تخریب (PR Diff Review)

قبل از تأیید (Merge) یک پول ریکوئست در تیم‌های نرم‌افزاری:
1. دیف گیت را با دستور زیر کپی کنید:
   ```bash
   git diff main...feature-branch
   ```
2. متن دیف را در بخش **Diff Impact & PR Review** داشبورد قرار دهید.
3. سیستم:
   - سمبل‌های دستکاری شده را شناسایی می‌کند.
   - گراف را تا ۳ سطح پیمایش کرده و توابع و فایل‌های نیازمند تست را لیست می‌نماید.
   - رتبه ریسک (`LOW`, `MEDIUM`, `HIGH`, `CRITICAL`) صادر می‌کند.
   - تحلیل معماری و برنامه تست رگرسیون را توسط مدل زبانی تدوین می‌نماید.

---

## ۶. مدیریت چند ریپازیتوری (Multi-Repository Hub)

سامانه کدهای پروژه‌های مختلف را با نودهای جداگانه `Repository` برچسب‌گذاری می‌کند. بنابراین:
- می‌توانید همزمان کدهای Microservices سازمان را ایندکس کنید.
- جستجوها می‌توانند محدود به یک ریپو یا در سراسر سازمان انجام شوند.
- با متد `DELETE /api/v1/repos/{repo_name}` تمام فایل‌ها، کلاس‌ها و روابط یک پروژه به صورت آبشاری از دیتابیس حذف می‌شوند بدون اینکه به سایر پروژه‌ها آسیبی برسد.

---

## ۷. راه‌اندازی وب‌هوک گیت‌هاب جهت ایندکس خودکار (CI/CD)

جهت بی‌نیازی از ایندکس دستی پس از هر کامیت:
1. به تنظیمات مخزن در GitHub رفته و به بخش **Settings > Webhooks > Add webhook** بروید.
2. آدرس Payload URL را تنظیم کنید:
   ```
   https://<your-server-ip-or-domain>:8001/api/v1/webhooks/github
   ```
3. فرمت را روی `application/json` قرار دهید.
4. با هر Push، گیت‌هاب یک پکت به این اندپوینت ارسال می‌کند و ورکر Celery کدهای جدید را کلون و ایندکس می‌نماید.

---

## ۸. آموزش اتصال ایجنت‌های هوش مصنوعی با پروتکل MCP

سرور اختصاصی MCP سامانه روی پورت **8002** (در حالت SSE) و همچنین از طریق stdio فعال است.

### ابزارهای فعال در FastMCP:
1. `search_code_graph`: جستجوی معنایی و ساختاری در کدبیس.
2. `get_function_details`: دریافت متن کامل سورس‌کد، مکان و داک‌استرینگ تابع.
3. `find_callers`: یافتن تمام صدازنندگان یک تابع.
4. `calculate_blast_radius`: محاسبه شعاع تخریب قبل از ریفکتور.
5. `detect_code_smells`: گزارش خودکار چرخه‌ها و توابع خدا.
6. `review_code_diff`: بررسی ساختاری یک Git Diff توسط ایجنت.
7. `list_repositories`: گزارش وضعیت و زبان مخازن موجود.
8. `get_codebase_stats`: آمار کلان نودها و روابط.
9. `index_local_repository`: اسکن یک پوشه محلی امن.
10. `clone_and_index_git_repository`: کلون و ایندکس مستقیم آدرس گیت.

### الف) اتصال ریموت با پروتکل SSE (پیشنهادی برای Cursor / Claude / Windsurf / Antigravity):
در تنظیمات MCP ایجنت خود، آدرس سرور را وارد کنید:
```json
{
  "mcpServers": {
    "codegraph-brain": {
      "url": "http://<SERVER_IP>:8002/sse"
    }
  }
}
```

### ب) اتصال مستقیم لوکال (Docker stdio):
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

---

## ۹. مستندات REST API و اندپوینت‌های کلیدی (Port 8001)

کلیه درخواست‌ها (به جز `/healthz`) نیازمند هدر `X-API-Key` هستند و از طریق پورت **8001** (پراکسی امن Nginx) سرو می‌شوند:

```bash
# بررسی سلامت سرویس‌ها
curl -X GET "http://localhost:8001/healthz"

# دریافت لیست ریپازیتوری‌ها
curl -X GET "http://localhost:8001/api/v1/repos" \
  -H "X-API-Key: your-api-key"

# گزارش بوی کد
curl -X GET "http://localhost:8001/api/v1/analytics/code-smells?repo_name=MyProject" \
  -H "X-API-Key: your-api-key"

# محاسبه شعاع تخریب یک تابع
curl -X GET "http://localhost:8001/api/v1/analytics/impact/parse_source_file" \
  -H "X-API-Key: your-api-key"

# بازبینی معماری دیف
curl -X POST "http://localhost:8001/api/v1/review/diff" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-api-key" \
  -d '{"diff_text": "diff --git a/... b/...", "repo_name": "MyProject"}'
```

---

## ۱۰. کوئری‌نویسی پیشرفته در دیتابیس Neo4j

از طریق آدرس `http://localhost:7474`:

### کشف چرخه‌های فراخوانی:
```cypher
MATCH (a:Function)
MATCH path = (a)-[:CALLS*2..3]->(a)
RETURN [n in nodes(path) | n.name] AS Cycle, length(path) AS Length
LIMIT 20;
```

### کشف توابع خدا با بیشترین فراخوانی ورودی و خروجی:
```cypher
MATCH (fn:Function)
OPTIONAL MATCH (caller:Function)-[:CALLS]->(fn)
OPTIONAL MATCH (fn)-[:CALLS]->(callee)
WITH fn, count(DISTINCT caller) AS InDegree, count(DISTINCT callee) AS OutDegree
WHERE InDegree > 3 OR OutDegree > 5
RETURN fn.name, fn.file_path, InDegree, OutDegree, (InDegree * 2 + OutDegree) AS RiskScore
ORDER BY RiskScore DESC
LIMIT 10;
```

---

## ۱۱. نکات امنیتی و عیب‌یابی (Troubleshooting)

1. **بررسی لاگ‌های خطای کانتینرها:**
   ```bash
   docker compose logs -f api
   docker compose logs -f celery_worker
   ```
2. **بررسی پایش سلامت سیستم:**
   اگر اندپوینت `/healthz` وضعیت `degraded` برگرداند، اتصالات به Neo4j یا Redis را بازبینی کنید.
3. **حفاظت در برابر اسکن‌های غیرمجاز:**
   سیستم به صورت خودکار پوشه‌های سیستمی، `.git`، `node_modules`، `venv` و کش‌های باینری را نادیده می‌گیرد تا سرعت و امنیت به حداکثر برسد.

---

<div align="center">
  <sub>CodeGraph Brain Enterprise — همراه مطمئن معماران نرم‌افزار و مهندسان ارشد</sub>
</div>

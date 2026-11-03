# Forager Faculty 🎓

**[简体中文](README.md)** | **English**

![Python](https://img.shields.io/badge/python-3.14%2B-blue.svg)
![Node](https://img.shields.io/badge/node-24-brightgreen.svg)
![uv](https://img.shields.io/badge/uv-managed-purple.svg)
![Tests](https://img.shields.io/badge/tests-424%20passed-brightgreen.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)

> A **forager** adjusts its path based on nectar signals — in the same way, this
> LLM-driven adaptive crawler iterates its extraction rules based on failure
> signals from its evaluator. Its collection target is **university faculty**:
> a 4-stage pipeline (schools → departments → teacher rosters → teacher
> details) collecting publicly published faculty information from Chinese
> university websites, built around an **evaluate → fix → re-evaluate loop**:
> the agent finds its own data-quality problems and repairs the spider with
> structured reasons — no manual spot-checking or hand-written rules.
>
> **Open-source data boundary**: this repository distributes data only up to the
> "department URL list" level (school attributes + department names / official
> URLs). The **collection framework and mechanisms for teacher rosters and
> teacher details are fully preserved**, but their data products are not
> distributed with the repo — see [Data Boundary](#data-boundary). Aggregate
> statistics of a full 144-school run:
> [docs/采集结果统计报告.md](docs/采集结果统计报告.md) (Chinese, with an
> English summary at the top).

## Table of Contents

- [Features](#features)
- [Architecture Overview](#architecture-overview)
- [Core Mechanisms](#core-mechanisms)
  - [The Evaluate-Fix-Re-evaluate Loop](#1-the-evaluate-fix-re-evaluate-loop-agent-kernel)
  - [Adaptive Extraction Rules](#1b-adaptive-extraction-rules-the-llm-designs-rules-rules-do-the-extracting)
  - [Adaptive Request Stack](#2-adaptive-request-stack-util)
  - [Site Archetype Routing](#3-site-archetype-routing-step-4-preflight)
  - [LLM Integration: Single Token / Token Pool](#4-llm-integration-single-token--dynamic-token-pool)
- [Quick Start](#quick-start)
- [Security & Data Boundary](#security--data-boundary)
- [Data Boundary](#data-boundary)
- [Repository Layout](#repository-layout)
- [Disclaimer](#disclaimer)
- [License](#license)

## Features

| | Feature | Description |
|---|---|---|
| 🔁 | **Evaluation-driven quality loop** | An 8-dimension evaluator scores every output; FAIL carries structured reasons into a repair loop (≤3 rounds), with rollback protection and deterministic template fallback |
| 🧩 | **Adaptive to any site structure** | The LLM reads page samples (DOM structure / links / text) and **designs extraction rules** — a deterministic spider of BeautifulSoup selectors + regex — instead of feeding every page to the LLM; rules re-run at zero token cost with reproducible results, and unseen sites work too |
| 📝 | **Battle-tested prompt system** | Every hard rule in the prompts is backed by a real measured failure; the repair loop turns each newly discovered failure mode into a prompt rule, evolving alongside the knowledge base |
| 🌐 | **5-level degradation request stack** | `curl_cffi` TLS fingerprint → `iv8`/`nodenv`/`sdenv` pure-CPU RiverSecurity JS challenge solving → browser rendering → resident CDP instance — automatic level-by-level fallback |
| ✅ | **Verify results, never fake success** | Every output must pass the evaluator (8 dimensions + hard gates) to count: FAIL carries structured reasons driving retries (≤3 rounds) + rollback protection; 144-school department crawl at 100% pass rate, 8,049 departments averaging 99.3; 424 offline unit tests guard the framework itself |
| 🔑 | **Dual-mode LLM integration** | Single token out of the box, zero config; optional token manager pool: auto-eviction of dead keys, round-robin allocation, sticky sessions |
| 🛡 | **Production-grade runtime** | LLM circuit breaker, 3-tier resource guard, watchdog self-healing, failed-department freeze ledger |

## Architecture Overview

### 4-Stage Pipeline

```
Step 1  School info ──── colleges info/
  │      Input:  MOE list / Gaokao portals / CHSI (public sources)
  │      Output: unified_colleges.csv (2,952 schools · 34 public columns)
  ▼
Step 2  Departments ─── colleges department/
  │      Input:  university websites → orchestrator.py (analyze → generate spider → run → evaluate → repair)
  │      Output: depart_985211.csv (144 schools · 8,049 departments: name + official URL)
  │      ★★★ open-source data boundary ends here ★★★
  ▼
Step 3  Teacher roster ─ department teachers/
  │      Input:  department faculty pages → collect_phases.py (Phases 1-5)
  │      Output: output/<school>/<dept>/teachers.csv (name + detail URL, generated at runtime)
  ▼
Step 4  Teacher details ── teachers/
         Input:  Step 3 roster → download_stage.py (roster QC + differential download)
         Output: teachers_detail.csv (13 fields, generated at runtime) + raw HTML/PDF archive
```

| Stage | Directory | Responsibility | Entry point |
|---|---|---|---|
| Step 1 | `colleges info/` | Merge 3 public sources into the national school list | per-source scrapers |
| Step 2 | `colleges department/` | Extract departments + URLs from each school site | `orchestrator.py` |
| Step 3 | `department teachers/` | Faculty pages → teacher roster | `collect_phases.py` / `evaluate_teachers.py` / `fix_loop.py` |
| Step 4 | `teachers/` | Teacher profile pages → 13-field structured details | `download_stage.py` / `extract_all_details.py` |

Stages are connected by a **pure file contract**: each stage's output CSV is the
only input of the next (`templates/contract.py` is the single source of truth
for column names). Any stage can be re-run independently — spider scripts live
next to their data products with a provenance hash, so the evaluator can
guarantee "data exists ⟺ a runnable spider exists".

### Every Stage Runs the Same Agent Loop

All four stages share one working pattern; only the evaluation dimensions and
spider shapes differ:

```
generate spider → run → evaluator scores multiple dimensions
    ▲                        │
    │                   PASS ✅ persist output
    │                   FAIL ❌ carry structured reasons
    │                        ▼
    └──── LLM repairs spider ←┘   (≤3 rounds; repeated failure → deterministic fallback template)
```

- **Step 2**: `orchestrator.py` — page-structure analysis → LLM generates a
  department spider → run → evaluate the department list
- **Step 3**: `collect_phases.py` (find faculty pages → LLM generates spider →
  run & verify → LLM evaluates → repair with reasons) → `evaluate_teachers.py`
  8-dimension evaluation → `fix_loop.py` repair loop
- **Step 4**: `download_stage.py` roster QC → `extract_all_details.py`
  LLM-first extraction (rules as fallback) → 13-field cross-validation

## Core Mechanisms

### 1. The Evaluate-Fix-Re-evaluate Loop (Agent Kernel)

Using Step 3 as the example (`department teachers/`; other stages are isomorphic):

```
 spider.py ──run──▶ teachers.csv
     ▲                    │
     │                    ▼
     │          evaluate_teachers.py (8 dimensions)
     │             ├─ PASS ✅ → persist + mark .fix_done
     │             └─ FAIL ❌ → structured failure reasons
     │                    │
     │                    ▼
     │          fix_loop.py repair loop (≤ 3 rounds)
     │             ├─ LLM reads failure reasons + page samples → improves spider
     │             ├─ rollback protection: restore the old spider if the new one is worse
     │             ├─ repeated LLM failure → deterministic template fallback (fix_generic.py)
     │             └─ successful cases feed the knowledge base, reused across departments
     │                    │
     └──── repaired spider ◀┘
              (back to run → re-evaluate, until PASS or a legitimate-empty terminal state)
```

**The 8 evaluation dimensions** (FAIL is not just a boolean — every failure
carries actionable reasons):

| # | Dimension | What it detects |
|---|------|---------|
| 1 | Invariant | a CSV exists ⟺ a runnable spider exists |
| 2 | Name format | 2-4 char Chinese names / alphabetic foreign-teacher names |
| 3 | Navigation words | hits a 320+ word NAV_WORDS list (column names mistaken for people) |
| 4 | Discipline words | column terms like "Choral Conducting" / "Music Theory" leaking in |
| 5 | Name concatenation | "X-某Technology" = name "X-某" + job-title suffix glued together |
| 6 | URL structure | too many institutional pages = category pages mistaken for teacher pages |
| 7 | Count sanity | strict recount of the faculty page; CSV < 30% of it → flagged too-low |
| 8 | Legitimate-empty exemption | administrative units / no-faculty-section marked as legitimately empty, not killed |

**Why this design**: crawler failure modes are endless (nav words as names,
glued fields, missed pagination, JS shell pages, PDF rosters …) — you cannot
enumerate rules for all of them. The evaluator formalizes "what collecting
correctly means", and the LLM only does one thing: "read the reasons, fix the
code". This separation of concerns lets the repair loop run unattended across
entire universities.

**The prompts were sedimented, not written**. Every `【2026-08-30 hardening】`
-style rule is backed by a class of real measured failures (missing names that
live in `title` attributes, whole pages dumped into a single field, historical
job titles mistaken for current ones …). Prompt templates interlock with the
failure-case knowledge base (`knowledge/`): each time the repair loop discovers
a new failure mode, it hardens it into a prompt rule so the next department
skips the pit — **which is why the framework adapts to site structures it has
never seen, instead of only working on the sites it was coded against**.

### 1b. Adaptive Extraction Rules: the LLM Designs Rules, Rules Do the Extracting

The "adaptive HTML reading" lands on a **generator, not an extractor**. For
each new department:

```
faculty-page HTML
  │  ① Sampling: DOM container structure (≤6 segments) + link samples (≤8)
  │     + body text (800 chars)
  ▼
  ② LLM reads the samples → designs extraction rules → emits spider.py
  │     · BeautifulSoup CSS selectors (container → name → detail URL)
  │     · regex assists (URL-shape checks, title stripping, pager links)
  │     · ≤80-line standalone script — deterministic, zero-token re-runs
  ▼
  ③ Hard gates (verify at generation time; fail → regenerate)
  │     · page has pager links but spider has no pagination loop → reject
  │     · after running: page has ≥10 teacher-level links but CSV URLs = 0 →
  │       reject (no plain-text exemption)
  │     · name-regex check: <80% valid 2-4-char Chinese names → score down;
  │       header drift auto-normalized
  ▼
  ④ Run → teachers.csv → 8-dimension evaluator → fail-with-reasons retry (≤3)
```

**Why LLM-generated rules instead of LLM-per-page extraction**:

| | LLM generates rules (this framework) | LLM extracts per page |
|---|---|---|
| Token cost for a 300-teacher department | 1 spider generation | 300 page extractions |
| Re-run / incremental refresh | zero LLM cost, locally reproducible | pay again per page |
| Result stability | deterministic script: same input, same output | sampling variance each run |
| Auditability | rules are code — readable, editable | extraction is a black box |
| Quality lever | evaluator watches the spider; rollback/repair | per-page fixes have no lever |

(Exception: Step 4 field extraction is LLM-per-page — deep semantic
understanding of 13 fields has no stable rules to write; college-level rules
remain the fallback when the LLM is unavailable, see `teachers/README.md`.)

Extraction rules sediment across departments into a **pattern library**
(`knowledge/teacher_patterns.json`, 8 page shapes: detail links / card
containers / plain-text name+title / numbered 4-line groups / PDF roster
tables / sub-page recursion …), injected as warm-start when generating the
next spider for the same school.

### 2. Adaptive Request Stack (`util/`)

Single entry point `smart_fetch(url)`, 5-level automatic degradation, returns
`(html, method)` with the level that hit:

```
 URL
  │  ① curl_cffi ——— browser TLS fingerprint (chrome131); one hop for WAF-free sites
  │  ② iv8 ——————— pure-CPU V8 environment patch, solves application-layer
  │                 RiverSecurity JS challenges (preferred cookie producer)
  │  ③ nodenv / sdenv — Node environment patch (hand-written zero-dep / full jsdom)
  │  ④ DrissionPage / Playwright —— headless rendering + RiverSecurity fallback
  │  ⑤ CDP bridge to a resident browser (:9222) —— network-layer RiverSecurity + full JS
  ▼
 (html, method)   —— all levels failed → blocked; system curl as TLS-fingerprint fallback
```

Supporting components:

| Component | Location | Role |
|---|---|---|
| RiverSecurity handling | `handleRuishu.py` / `rs_cookie.py` / `iv8_rs.py` | auto-detect 412/202, `$_ts` markers; per-domain cookie cache + regeneration on invalidation |
| TLS fingerprint rotation | `fingerprint_chain.py` | chrome/firefox/safari multi-fingerprint switching |
| Browser pools | `drission_pool.py` / `cdp_util.py` | window×tab pools, resident CDP reuse, idle reaping (`browser_lifecycle.py`) |
| Breakers & guards | `llm_gate.py` / `resource_guard.py` | LLM sliding-window circuit breaker; 3-tier (green/yellow/red) system-load concurrency control |

> Full anti-RiverSecurity design — the 3-layer adversarial model, how the three
> pure-CPU solvers (iv8 / nodenv / sdenv) compare, cookie lifecycle, concurrency
> safety and a troubleshooting manual — see the dedicated doc
> **[docs/瑞数反爬解决方案.md](docs/瑞数反爬解决方案.md)** (Chinese, with an
> English summary at the top).

### 3. Site Archetype Routing (Step 4 Preflight)

The "traps" of teacher profile pages belong to a finite family. Before
downloading, Step 4 classifies each department by preflight and routes it to a
per-archetype strategy instead of a one-size-fits-all crawl:

| Archetype | Signature | Strategy |
|---|---|---|
| `normal` | has URLs and normal content density | standard pipeline |
| `pdf_shell` | page references PDFs (player/iframe/link) | PDF detection + download + adaptive extraction |
| `empty_shell` | content-sample tag density ≈ 0 (JS shell) | rendering fallback + skip-retry when shells dominate |
| `name_list_no_links` | ≥50% teachers lack detail URLs | search-based discovery (Baidu/Bing, ≤10 queries/dept) + roster persistence |

### 4. LLM Integration: Single Token / Dynamic Token Pool

| | Mode A (default): single token | Mode B (optional): token pool |
|---|---|---|
| Config | `.env`: `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | `token manager/tokens.json` with multiple keys |
| Compatible with | Anthropic / DeepSeek / any OneAPI-style gateway | same |
| Failure handling | errors stop the run | failed keys switch automatically; monthly quota probing, auto-eviction |
| Allocation | — | round-robin + least-used-first; sticky sessions (same dept → same key, better prompt-cache hits) |
| Backend | — | Redis atomic round-robin (`redis://localhost:6379/0`); JSON file fallback for single machines |

```bash
cp "token manager/tokens.example.json" "token manager/tokens.json"
# edit in your keys, then:
uv run --project . python "token manager/token_pool.py" check      # probe every key's quota
uv run --project . python "token manager/token_pool.py" list       # show token status
```

The LLM client (`util/llm_client.py`) **auto-enables pool mode** when it
detects `tokens.json` — no code changes needed.

> Full LLM-layer design — key/base_url/model resolution priority, failure
> switching and error classification, the pool's 3-tier allocation strategy
> (session-sticky / dept-affinity / least-used), probing and self-healing,
> two-layer concurrency semaphores, circuit breaker — see the dedicated doc
> **[docs/LLM接入与令牌池.md](docs/LLM接入与令牌池.md)** (Chinese, with an
> English summary at the top).

## Quick Start

```bash
# 0. Requirements: Python >= 3.14 (managed by uv), Node.js 24 (only for the RiverSecurity sdenv path)
uv sync
npm ci                # only needed by util/rsenv/ (RiverSecurity env patch); skip if unused

# 1. Configure (Mode A: three lines get the core running)
cp .env.example .env  # LLM_API_KEY / LLM_BASE_URL / LLM_MODEL

# 2. Step 3 teacher roster (example: all departments of Soochow University; --dept 商学院 for a single one)
uv run --project . python "department teachers/collect_phases.py" --school 苏州大学
uv run --project . python "department teachers/evaluate_teachers.py" 苏州大学   # evaluate
uv run --project . python "department teachers/fix_loop.py" 苏州大学 --resume   # repair FAILs

# 3. Step 4 teacher details (download + extract)
uv run --project . python teachers/download_stage.py --all --teacher-workers 5
uv run --project . python teachers/extract_all_details.py

# 4. Unit tests (no network / no LLM / no browser)
uv run --project . python -m unittest discover -s tests
```

Outputs land in `department teachers/output/<school>/<dept>/teachers.csv` and
`teachers/<school>/` (both `.gitignore`d — nothing is committed by accident).
All tunable parameters are in `.env.example` (~50 entries, grouped by LLM /
proxy / browser / anti-WAF / resource guard / pipeline, with comments).## Security & Data Boundary

**Pre-release security audit** (each item executed before open-sourcing):

| Check | Result |
|---|---|
| Key leakage (`sk-`, API keys, tokens, passwords — full-repo scan) | ✅ zero hits; no real keys anywhere — everything is injected via `.env` / `tokens.json` (both gitignored) |
| Intranet addresses (`*.baidu-int.com` / private IP ranges) | ✅ zero hits; gateway addresses come only from the `LLM_BASE_URL` env var, no defaults in code |
| Teacher personal info (test fixtures, knowledge base, samples) | ✅ all pseudonymized / placeholder-ized (`某教师`, `example.edu.cn`, real phones zeroed) |
| Hardcoded machine paths (`/Users/...`) | ✅ zero hits; everything derives from `Path(__file__)` |
| Log & console output | ✅ keys always masked (first 8 chars + `...` + last 4, `mask_sk`) |

**Data tiering**: repository distribution stops at the "department URL list"
level; teacher-level data (rosters / details) can only be collected by users
running the framework themselves, and all framework output directories are
gitignored — there is no accidental-commit path.

## Data Boundary

| Level | Distributed with the repo? |
|---|---|
| School info (name / affiliation / 985/211 / rankings and other public attributes) | ✅ `colleges info/unified_colleges.csv` |
| Department list (school → department name + department website URL) | ✅ `colleges department/depart_985211.csv` + `depart_985211.jsonl` |
| Teacher roster (name + detail URL) | ❌ not distributed; run the `department teachers/` framework yourself |
| Teacher details (13 fields + raw pages) | ❌ not distributed; run the `teachers/` framework yourself |

All runtime output directories (`colleges department/output/`,
`department teachers/output/`, `teachers/<school>/`) are excluded via
`.gitignore` — **spider scripts, evaluation reports and collected data are all
generated at runtime and never distributed**; the repository contains only the
validated collection framework itself.

## Repository Layout

```
├── colleges info/             # Step 1: school info (3-source merge, 2,952 schools)
├── colleges department/       # Step 2: departments (framework + 8,049 dept URLs — the open-source data boundary)
├── department teachers/       # Step 3: teacher-roster collection agent
│   ├── collect_phases.py      #   main pipeline (find pages → gen spider → run → evaluate → repair)
│   ├── evaluate_teachers.py   #   8-dimension evaluator
│   ├── fix_loop.py            #   repair loop (rollback / resume / knowledge-base learning)
│   ├── fix_generic.py         #   deterministic collector (fallback when LLM keeps failing)
│   ├── templates/             #   spider templates + data contract (contract.py)
│   ├── knowledge/             #   page-structure pattern knowledge base (self-evolving)
│   └── script/                #   orchestration / self-healing / watchdog / reporting scripts
├── teachers/                  # Step 4: teacher-details collection agent
│   ├── download_stage.py      #   download stage (roster QC + differential download + archetype routing)
│   ├── extract_all_details.py #   extract stage (LLM-first, rules as fallback)
│   ├── detail_scraper.py      #   core scraper (PDF / rendering fallback / classification)
│   └── scripts/               #   batch differential engine / per-dept rule tools
├── token manager/             # LLM token pool (optional load balancing, example structure only)
├── util/                      # shared request stack (smart_fetch 5-level / RiverSecurity / browser pools / LLM gateway)
├── tests/                     # 424 unit tests (run offline)
├── .env.example               # config template (~50 entries, commented)
└── pyproject.toml             # uv project definition
```

Design details for each stage live in that directory's `README.md` / `DESIGN.md`.

## Disclaimer

- This framework is intended **only** for information published openly on
  university websites. Respect each site's robots.txt, terms of service, and
  applicable local laws; keep crawl rates and concurrency restrained (the
  framework ships with throttling and resource guards — please do not raise
  them to levels that stress the target sites)
- Teacher personal information is legally protected (e.g., by China's Personal
  Information Protection Law). Storage, use, and sharing of any personal
  information you collect with this framework is **your** responsibility;
  **do not use the collected data for any illegal purpose or in ways that
  infringe others' rights**
- This repository does not contain or distribute any collected teacher
  personal data; `token manager/` contains only pool-management code and an
  example structure — no real keys

## License

[MIT](LICENSE)

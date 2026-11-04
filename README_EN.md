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
> details) collecting publicly published faculty information from university
> websites, built around an **evaluate → fix → re-evaluate loop**:
> the agent finds its own data-quality problems and repairs the spider with
> structured reasons — no manual spot-checking or hand-written rules.
>
> **Open-source data boundary**: this repository distributes data only up to the
> "department URL list" level (school attributes + department names / official
> URLs, plus the QS overseas university list). The **collection framework and
> mechanisms for teacher rosters and teacher details are fully preserved**, but
> their data products are not distributed with the repo — see
> [Data Boundary](#data-boundary). Aggregate statistics of a full 144-school run:
> [docs/采集结果统计报告.md](docs/采集结果统计报告.md) (Chinese, with an
> English summary at the top).

## Table of Contents

- [Features](#features)
- [Why Forager](#why-forager)
- [Architecture Overview](#architecture-overview)
- [Track Record](#track-record)
- [Core Mechanisms](#core-mechanisms)
- [Typical Site Cases](#typical-site-cases)
- [Quick Start](#quick-start)
- [Documentation Index](#documentation-index)
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

## Why Forager

| Traditional crawling | Forager |
|---|---|
| One hardcoded set of CSS selectors per site; breaks on every redesign | LLM reads page samples and **generates extraction rules** (BeautifulSoup selectors + regex) — unseen sites get fresh rules on the spot |
| Manual spot-checks find missing/wrong data after the fact | An **8-dimension evaluator** scores every output; FAIL carries structured reasons into a repair loop (≤3 rounds + rollback + deterministic fallback) |
| Extraction is a black box; results don't reproduce; rules can't be audited | Rules are **≤80-line deterministic scripts**: same input, same output; zero-token re-runs; human-readable and editable |
| One anti-bot wall and everything stops; maintenance is firefighting | **5-level degradation request stack** + three pure-CPU RiverSecurity solvers (iv8 / nodenv / sdenv), automatic fallback |

In one line: **turn "how to write a crawler" into "how to judge a crawler"** —
both rule generation and rule verification run in an agent loop; humans only define
what "collected correctly" means. That also means the framework grows with its case
library: every newly fixed site type is a pit the next university skips (see
[Typical Site Cases](#typical-site-cases)).

## Architecture Overview

### 4-Stage Pipeline

```
Step 1  School info ──── colleges info/
  │      Input:  MOE list / Gaokao portals / CHSI (public sources)
  │      Output: unified_colleges.csv (2,952 schools · 34 public columns)
  │      ＋ qs_oversea/: QS 2027 overseas list (1,504 schools, 100% website-verified)
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
| Step 1 | `colleges info/` | Merge public sources into the national school list + QS overseas list | per-source scrapers |
| Step 2 | `colleges department/` | Extract departments + URLs from each school site | `orchestrator.py` |
| Step 3 | `department teachers/` | Faculty pages → teacher roster | `collect_phases.py` / `evaluate_teachers.py` / `fix_loop.py` |
| Step 4 | `teachers/` | Teacher profile pages → 13-field structured details | `download_stage.py` / `extract_all_details.py` |

Stages are connected by a **pure file contract**: each stage's output CSV is the
only input of the next (`templates/contract.py` is the single source of truth
for column names). Any stage can be re-run independently — spider scripts live
next to their data products with a provenance hash, so the evaluator can
guarantee "data exists ⟺ a runnable spider exists".

### Every Stage Runs the Same Agent Loop

```
generate spider → run → evaluator scores multiple dimensions
    ▲                        │
    │                   PASS ✅ persist output
    │                   FAIL ❌ carry structured reasons
    │                        ▼
    └──── LLM repairs spider ←┘   (≤3 rounds; repeated failure → deterministic fallback template)
```

All four stages share this pattern; only the evaluation dimensions and spider
shapes differ — see [docs/核心机制.md](docs/核心机制.md).

## Track Record (full 144-school run)

| Stage | Scale | Quality verification |
|---|---|---|
| Schools → departments | 2,952 schools filtered → 144 schools / **8,049 departments** (name + official URL) | 4-dimension evaluator pass rate **100%**, average score **99.3** (131 schools perfect) |
| Departments → teacher rosters | **5,813 department rosters · 405,583 teachers** | 8-dimension evaluation + hard gates + repair loop; nothing lands without PASS |
| Rosters → teacher details | **359,147 detail rows**, 83.3% with ≥1 of 13 fields | field-misalignment cross-checks; download/extract decoupled — re-extract without re-fetching |
| Raw archive | **43 GB**: 375,564 HTML + 4,447 PDF + 67,883 photos | differential download — never re-fetch unchanged content |

- 16 unconventional-site cases fixed and archived: names living in `title`
  attributes (Tsinghua AI Institute), JS-rewritten links (PKU School of Economics),
  a 43-page PDF roster (CQU-UC joint institute), a site-wide AES-GCM shell (ZUEL)…
- **424 offline unit tests** guard the framework itself (no network / no LLM / no
  browser required for a full regression run)
- Methodology and verification system:
  [docs/采集结果统计报告.md](docs/采集结果统计报告.md) (Chinese, with an
  English summary at the top)

## Core Mechanisms

Deep dives are split into dedicated documents (Chinese, each with an English
summary at the top):

| Document | Covers |
|---|---|
| [docs/核心机制.md](docs/核心机制.md) | The evaluate-fix-re-evaluate loop (8-dimension table), adaptive extraction rules (LLM designs rules / hard gates / pattern library), site archetype routing |
| [docs/瑞数反爬解决方案.md](docs/瑞数反爬解决方案.md) | The 5-level request stack, 3-layer adversarial model, the three pure-CPU solvers (iv8/nodenv/sdenv), cookie lifecycle, troubleshooting manual |
| [docs/LLM接入与令牌池.md](docs/LLM接入与令牌池.md) | Single token / dynamic token pool, key resolution priority, failure switching, 3-tier allocation, circuit breaker |

## Typical Site Cases

Of 5,287+ department sites, the vast majority are standard CMS structures;
a minority of unconventional sites were once wrongly judged "unreachable / no
faculty / JS shell". Each case hardened into a repair mechanism — one
representative per family:

| Site | Quirk | Countermeasure |
|---|---|---|
| Chongqing University–UC Joint Co-op Institute | the roster lives in a **43-page PDF**; zero names in HTML, rendering can't help | dedicated PDF extraction |
| Chongqing University, Civil Engineering | homepage is a **language-selector page**; image-only links, no anchor text | entry-selector follow (61→263 people) |
| Peking University, School of Economics | static hrefs are **JS-rewritten relative links**; naive joins 404 | render to capture real hrefs |
| Zhongnan University of Economics and Law | site-wide **AES-GCM shell** with no RiverSecurity markers; returned as success | pure-CPU shell cracking |
| Chang'an University | every teacher gets a **`list.htm` detail page** (only 0.9% of 6,291 are real lists) | exemption heuristic |
| Peking University, CS | **letter index overrides numeric pagination**; half the teachers missed | BFS pagination closure (26→133 people) |

Full case catalog (16 cases · 6 families + anti-misjudgment discipline):
[docs/特殊网站结构案例库.md](docs/特殊网站结构案例库.md) (Chinese, with an
English summary at the top).

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
proxy / browser / anti-WAF / resource guard / pipeline, with comments).

## Documentation Index

| Document | Covers |
|---|---|
| [docs/核心机制.md](docs/核心机制.md) | Agent loop / rule generation / hard gates / archetype routing |
| [docs/特殊网站结构案例库.md](docs/特殊网站结构案例库.md) | Unconventional site-structure catalog (16 cases · 6 families + discipline) |
| [docs/瑞数反爬解决方案.md](docs/瑞数反爬解决方案.md) | Request stack & RiverSecurity countermeasures |
| [docs/LLM接入与令牌池.md](docs/LLM接入与令牌池.md) | LLM integration modes & token pool |
| [docs/安全与数据边界.md](docs/安全与数据边界.md) | Release audit record, data tiering, compliance workflow |
| [docs/采集结果统计报告.md](docs/采集结果统计报告.md) | Aggregate statistics of the 144-school full run |
| [`colleges info/README.md`](<colleges info/README.md>) | Step 1 school info (3-source merge + QS overseas list) |
| [`colleges department/README.md`](<colleges department/README.md>) | Step 2 departments |
| [`department teachers/README.md`](<department teachers/README.md>) | Step 3 teacher roster (full workflow) |
| [`teachers/README.md`](teachers/README.md) | Step 4 teacher details (two-stage decoupling); also `teachers/DESIGN.md`, `quality_criteria.md`, `dept_rule_format.md`, `concurrency_playbook.md`, `resource_strategy.md` |

## Data Boundary

| Level | Distributed with the repo? |
|---|---|
| School info (name / affiliation / 985/211 / rankings and other public attributes) | ✅ `colleges info/unified_colleges.csv` |
| Overseas university list (QS 2027: name / rank / official website) | ✅ `colleges info/qs_oversea/qs_rankings_2027_with_website.csv` (1,504 schools, 100% website-verified) |
| Department list (school → department name + department website URL) | ✅ `colleges department/depart_985211.csv` + `depart_985211.jsonl` |
| Teacher roster (name + detail URL) | ❌ not distributed; run the `department teachers/` framework yourself |
| Teacher details (13 fields + raw pages) | ❌ not distributed; run the `teachers/` framework yourself |

All runtime output directories (`colleges department/output/`,
`department teachers/output/`, `teachers/<school>/`) are excluded via
`.gitignore` — **spider scripts, evaluation reports and collected data are all
generated at runtime and never distributed**; the repository contains only the
validated collection framework itself. The pre-release audit record and the
pre-commit compliance workflow: [docs/安全与数据边界.md](docs/安全与数据边界.md).

## Repository Layout

```
├── colleges info/             # Step 1: school info (3-source merge, 2,952 schools)
│   └── qs_oversea/            #   QS 2027 overseas list (1,504 schools + website verification chain)
├── colleges department/       # Step 2: departments (framework + 8,049 dept URLs — the open-source data boundary)
├── department teachers/       # Step 3: teacher-roster collection agent
│   ├── collect_phases.py      #   main pipeline (find pages → gen spider → run → evaluate → repair)
│   ├── evaluate_teachers.py   #   8-dimension evaluator
│   ├── fix_loop.py            #   repair loop (rollback / resume / knowledge-base learning)
│   ├── templates/             #   spider templates + data contract (contract.py)
│   └── knowledge/             #   page-structure pattern knowledge base (self-evolving)
├── teachers/                  # Step 4: teacher-details collection agent (two-stage decoupling)
├── token manager/             # LLM token pool (optional load balancing, example structure only)
├── util/                      # shared request stack (smart_fetch 5-level / RiverSecurity / browser pools / LLM gateway)
├── docs/                      # core mechanisms / request stack / LLM integration / security & boundary / stats report
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

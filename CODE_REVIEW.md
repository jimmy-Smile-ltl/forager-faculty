# 代码审查报告 — Agent 管道（Step 3 + Step 4）

> 审查时间: 2026-08-18
> 审查范围: `department teachers/`（fix_loop.py / fix_generic.py / evaluate_teachers.py 含未提交改动、collect_teacher.py、templates/spider_template.py）+ `teachers/`（dept_agent.py / spider_gen.py / detail_scraper.py / faculty_discovery.py / extract_all_details.py / batch_run.py）+ `util/http_client.py`
> 状态: 审查完成，7 项确认缺陷已修复（见文末"已实施修复清单"），其余为建议

---

## 发现总览

| 级别 | 数量 | 状态 |
|------|:---:|------|
| 🔴 高 | 3 | 2 已修复，1 建议 |
| 🟠 中 | 6 | 2 已修复，4 建议 |
| 🟡 低 | 7 | 3 已修复，4 建议 |

---

## 🔴 高

### H1. fix_loop.py 用裸 `python3` 跑 spider → 依赖环境必然 ImportError（已修复）

`fix_loop.py:380` 与 `fix_loop.py:467` 用 `subprocess.run(["python3", "spider.py"], ...)` 运行生成的 spider。

生成的 spider 依赖 `bs4 / curl_cffi / requests`（见 `spider_template.py` 的 `FETCH_BLOCK`）。本机 `python3` 解析到 `/opt/homebrew/bin/python3`（Homebrew 系统解释器），实测 **不包含这些依赖**；项目按 uv 管理环境规范运行。而 `fix_generic.py:378` 用的是 `sys.executable`（当前解释器 = uv venv python）——同一项目两种写法，`fix_loop` 是错的。

后果：在 uv 环境下跑 `fix_loop.py`，spider 子进程 ImportError → 无 teachers.csv → 每轮都 FAIL，循环完全不可用（表面症状会被 `except` 吞掉，只剩 "spider未生成teachers.csv"）。

**修复**: 两处 `"python3"` → `sys.executable`。

### H2. fix_loop.py 回滚不恢复 spider_meta.json / teachers.json → 溯源校验必然误报（已修复）

R3 改进轮中，新 spider 试运行后若质量明显更差（`fix_loop.py:487-492`），只回滚了 `spider.py` 和 `teachers.csv`：

- `spider_meta.json` 仍记录**新** spider 的 hash；
- 下一轮 `evaluate_dept` 的产物溯源检查（`evaluate_teachers.py` 1a）拿新 meta 的 hash 对比回滚后的旧 spider.py → **必然报"产物溯源失败"**，把正常回滚误判成数据污染。

**修复**: 回滚时同时恢复 `spider_meta.json`（写回旧 spider 的 hash）与 `teachers.json`（旧内容）。

### H3. `util/cookies/*.json` 与 `util/.chrome_profile/`（615 个文件）被 git 跟踪（建议）

瑞数 session cookie 与完整 Chrome profile 已提交进 `origin/main`。对照工作区 CLAUDE.md「敏感信息保护」：cookie 属"配置凭证"性质，建议移出版本控制：

```bash
git rm -r --cached "util/cookies" "util/.chrome_profile"
# 在仓库根 .gitignore 追加:
# util/cookies/
# util/.chrome_profile/
```

> 未自动执行：属仓库卫生决策，且改动会删除远端文件，需你确认。

---

## 🟠 中

### M1. fix_loop 确定性模板的 evidence.anchor 是自引用 → LLM 人名终审的锚点证据形同虚设（已修复）

`fix_loop.py:137` 写入 `"evidence": {"page": ..., "anchor": n, ...}`，其中 `n` 是 **clean_name 清洗后的名字本身**（`extract_teachers` 里 `name = clean_name(t)`）。而 `evaluate_teachers.py` LLM 终审的判断逻辑是：

```python
a = anchors.get(n, "")
if a and a != n:   # ← anchor 永远 == name，永远不成立
    items.append(f'"{n}" (页面原文: "{a}")')
```

设计意图是给 LLM 传**原始链接文本**（如 `"陈招斌 高级工程师"` 或截断粘连的 `"陈招斌高"`），让 LLM 判断切分是否正确；当前实现导致该功能 100% 空转，LLM 只能凭孤词猜。

**修复**: evidence.anchor 改为提取时的原始 `<a>` 文本（含职称粘连），文本回退分支用原始行。

### M2. fix_loop 的 spider 运行超时硬编码 60s（已修复为可配置）

`fix_loop.py:380/467` `subprocess.run(..., timeout=60)`。慢站点 + Playwright 兜底（`FETCH_BLOCK` 内含浏览器渲染）很容易超 60s；超时后无 teachers.csv → `restore_best(); return "FAIL"`，无重试，慢站必死。

**修复**: `timeout` 改为环境变量 `SPIDER_TIMEOUT`（默认 120s），与 Step 4 `spider_gen.run_spider_script` 的 `SPIDER_TIMEOUT` 命名一致。

### M3. "网页结构分析"存在三份重复实现（建议重构）

`fix_loop.py` R0 内联分析、`fix_generic.analyze_page_structure`、`probe_site.analyze_dept` 三段功能几乎相同（首页入口发现 / 分组标题 / 教师级链接 / URL 模式 / 陷阱 / 分页 → 写 analysis.md）。目前三处已经出现细微分歧（如 URL 模式正则不同），后续改一处漏两处。

**建议**: 抽公共模块 `department teachers/analyze_site.py`（`analyze_site(dept_url, fac_url) -> analysis_md_lines`），三处调用同一实现。

### M4. evaluate_teachers 的 LLM 人名终审成本随评估轮次翻倍 + 零容忍误判风险（建议）

`evaluate_dept` 内对全员跑 LLM 终审（每批 60 人、90s 超时），而 `fix_loop` 每轮都调 `evaluate_dept`。2879 个待修院系 × 多轮 = LLM 调用量巨大。且"零容忍"下 LLM 一次误判（把真实姓名判成非人名）会让该院系永远 FAIL、永远进修复循环。

**建议**:
1. 只对**规则存疑**条目（anchor≠name、SUBJECT_HINT/GLUE_HINT 命中、2 字名）送 LLM 终审；无争议条目跳过 → 调用量降一个量级；
2. 终审结果按 `(school, dept, name)` 缓存到 `output/.llm_name_cache.json`，同名校重跑不再重复调用。

### M5. 超时后的 LLM 调用线程滞留后台（ThreadPoolExecutor 资源泄漏）（建议）

`fix_loop.llm_ask_with_timeout` 与 `evaluate_teachers` LLM 终审都用 `ThreadPoolExecutor + shutdown(wait=False, cancel_futures=True)`。`future.result(timeout=...)` 超时后**正在执行**的线程并不会被取消，而是作为守护线程继续持有 HTTP 连接直到完成；LLM 持续故障时每个超时调用都会留下一个滞留线程，逐院系累积。

**建议**: 模块级复用单个 `ThreadPoolExecutor(max_workers=1)`（不 shutdown），超时只放弃等待；或维护滞留线程计数，超过 N 时跳过 LLM 走规则判定。

### M6. `evaluate_teachers.py` 1c CSV/JSON 一致性在表头未匹配时静默跳过（建议）

`name_idx is None` 时 `csv_n` 恒为 0，检查被 `if csv_n > 0` 短路——表头识别失败（如列名变了）没有任何告警。建议表头未匹配时 append 一条 reason（"CSV表头未识别姓名列"），而不是静默通过。

---

## 🟡 低

### L1. dept_agent.py 无 LLM 兜底模板的 sys.path 少一层（已修复）

`dept_agent.generate_spider`（非 LLM 兜底路径，`dept_agent.py:319`）注入 `Path(__file__).resolve().parent.parent`——对位于 `teachers/<学校>/<院系>/spider.py` 的脚本，`parent.parent` = `teachers/<学校>`，而 `detail_scraper.py` 在 `teachers/` 根，import 必然失败。应为 `parent.parent.parent`。

### L1b. `faculty_discovery._is_detail_url` 与其它三处 DETAIL_HINT 正则不一致（已修复）

`faculty_discovery._is_detail_url` 只认 `/c\d+a\d+/` 形式，缺 `/c[0-9a-f]{6,}/`（`evaluate_teachers.count_teacher_elements`、`probe_site.DETAIL_PAT`、`spider_template` 均用十六进制形式）→ 详情页 URL 形如 `/c123456/` 的站点在 Step 4 师资发现时漏提取。已补上十六进制分支（只拓宽匹配，不缩小）。

### L1c. `evaluate_teachers.count_teacher_elements` 卡片分支重复计数（已修复）

卡片容器（`div.pic-item` 等）内的 `<a>` 通常也满足锚点循环的条件 → 同一教师被统计两次，教师级元素上界虚高 → "数量明显偏少"误判风险。已用 `seen` 去重（卡片分支的定位用途保留：名字在 span/img alt 里、锚点循环没计到的卡片仍会计入）。

### L2. `spider_template.FETCH_BLOCK` 的 `parents[4]` 深度假设脆弱（建议）

`spider_template.py:12` `_Path(__file__).resolve().parents[4]` 依赖 `output/<学校>/<院系>/spider.py` 恰好 4 层。结构目前固定成立，但建议改为向上查找含 `util/` 的祖先（或加注释说明深度假设），避免未来目录调整时静默失效。

### L3. `detail_scraper._extract_avatar_url` 策略 1 的 `img[src*='pic']` 过宽（建议）

可能抓取正文装饰图当头像。建议加尺寸/竖图比例二次校验（策略 3 已有，可复用）。

### L4. 无 `tests/` 目录，纯逻辑零覆盖（已补）

`clean_name / _parse_json / _detect_pdf / PDF 表格压平 / 分层抽样 / 人名过滤 / 评估器正则` 等纯逻辑此前没有任何单元测试，回归全靠肉眼。本次已新增 `tests/`（见文末）。

### L5. `dept_agent.evaluate` 的 `pdf_rate` 在无 pdf_rows 时返回 1.0（建议）

`dept_agent.py:414` `pdf_rate = pdf_url_ok / len(pdf_rows) if pdf_rows else 1.0`——没有 PDF 时按满分算合理，但 HTML 型院系的评分里 `20 * pdf_rate` 变成了送分项，与字段分权重失衡（HTML 页占比高的院系评分虚高）。建议按页面类型口径给权重（HTML 页 PDF 分并入采集分）。

---

## 已实施修复清单

| # | 文件 | 改动 |
|---|------|------|
| 1 | `department teachers/fix_loop.py` | `python3` → `sys.executable`（2 处）；spider 超时改环境变量 `SPIDER_TIMEOUT`（默认 120s） |
| 2 | `department teachers/fix_loop.py` | 回滚同时恢复 `spider_meta.json`（旧 hash）+ `teachers.json`；spider 运行前快照三者 |
| 3 | `department teachers/fix_loop.py` | 确定性模板 `evidence.anchor` 改为原始链接文本（含职称粘连），文本回退分支用原始行 |
| 4 | `teachers/dept_agent.py` | 无 LLM 兜底模板 `sys.path` 从 `parent.parent` 修正为 `parent.parent.parent` |
| 5 | `teachers/faculty_discovery.py` | `_is_detail_url` 补 `/c[0-9a-f]{6,}/` 十六进制详情URL形式（与其它 DETAIL_HINT 对齐） |
| 6 | `department teachers/evaluate_teachers.py` | `count_teacher_elements` 卡片分支按 href 去重，消除与锚点循环的重复计数 |
| 7 | `tests/`（新增） | `test_step3_logic.py` + `test_step4_logic.py` 共 45 个单元测试（unittest，无网络），全部通过 |

## Step 3 Agent 完善（2026-08-18 追加落地）

针对"Agent 设计缺点"分析中的 P0/P1 项，在 `department teachers/` 下落地（严格限定 Step 3）：

| # | 文件 | 改动 | 解决 |
|---|------|------|------|
| 1 | `templates/contract.py`（新） | 数据契约单一事实来源：CSV 规范 8 列 / JSON 结构 / 39 种历史列名映射表 | 字段不统一根因 |
| 2 | `normalize_teachers_output.py`（新） | 存量归一化：`--dry-run/--check/--school`，自动备份；**编码嗅探**（GBK/GB18030 历史文件正确解码修复，latin-1 脏文件跳过） | 39 种表头 → 规范 8 列 |
| 3 | `evaluate_teachers.py` | 1d 表头契约校验：姓名列/详情URL列识别不出 → FAIL + 提示 normalize | M6 |
| 4 | `evaluate_teachers.py` | LLM 人名终审降本：规则已 FAIL 跳过（不再重复调用）；结果缓存 `output/.llm_name_cache.json`（fix_loop 多轮重评第 2 轮起命中） | M4 前半 |
| 5 | `collect_teacher.py` | `load_org_keywords` 优先读 `knowledge/teacher_patterns.json`（load_kb 从死代码变为生效路径） | 知识库启用 |
| 6 | `fix_loop.py` | 评估 FAIL 时 `record_case` 沉淀失败案例到知识库（学习闭环开始运转） | 学习闭环 |
| 7 | `fix_loop.py` | `llm_improve_spider` 提示词注入历史失败案例（最近 3 条） | 跨院系记忆 |
| 8 | `fix_loop.py` | `--resume` 断点（PASS 写 `.fix_done` 标记跳过）；失败队列 `output/_fix_loop_queue.json` + `--queue/--clear-queue`（FAIL 自动入队，PASS 移出） | 断点续传/重排队 |
| 9 | `tests/test_normalize.py` + `test_fix_loop_queue.py`（新） | 13 个用例：归一映射/BOM/GBK 修复/脏文件跳过/队列增删清/缓存读写 | 回归保护 |

> 全部通过 `py_compile` + 58 个单元测试。未动 `teachers/`（Step 4）。

### 存量归一说明（重要）

- `normalize_teachers_output.py --check` 实测：**3,547/6,500 个院系不符合规范契约**（老版本表头），与"推倒重来"统计吻合
- 归一前务必 `--dry-run` 看统计；正式跑会自动备份到 `output/_normalize_backup/`
- 编码嗅探：历史文件存在 GBK 编码（如复旦英文表头文件），旧版若以 utf-8 读会乱码写回——新脚本先 `utf-8-sig → gb18030` 试读，仅对无法确认的 latin-1 脏文件跳过不改

## 后续建议优先级

1. M3 抽取公共网页结构分析模块（三处重复 → 一处）
2. M4 LLM 终审降本（规则存疑才送审 + 结果缓存）
3. H3 cookies/chrome_profile 移出版本控制（需你确认）
4. 跑一次全量 `evaluate_all.py` 验证评估器 3.0 新规则（analysis.md 缺失、零容忍、溯源）的实际波及面

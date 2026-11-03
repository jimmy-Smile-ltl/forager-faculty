# 采集质量准则（Ω 规格）— 单一事实来源

> 递归自优化系统（RSO）中的 **Ω**：理想目标/评估准则的显式规格。
> 评估器（dept_agent.evaluate）与批后回顾（batch_run.retrospective）
> 都以此文件为参照，禁止在代码/提示词里另写一套口径。
> 修改本文件 = 修改系统的理想目标，需同步确认 evaluate() 的阈值常量。

## 1. 采集完成度

- 状态判定单一口径: `detail_scraper.classify_record` → ok / pending_pdf / failed / no_url / unknown
- no_url（无详情页纯名单）与 pending_pdf（有 PDF 引用未落地）是**合法状态**，不计失败
- 阈值: **成功率 ≥ 90%**（成功率 = (ok + pending + no_url) / 总数）
- pending_pdf 必须 `pdf_url` 已记录，否则视为失败类问题

## 2. PDF 落地

- pdf_shell 页面必须记录 pdf_url：**记录率 ≥ 90%**（理想 100%）
- PDF 落地判据: `pdf_path` 非空（has_pdf=True 只表示"检测到引用"，不算落地）
- 下载失败走重试（RETRY_MAX 次退避），仍失败 → pending，可在 RESUME=1 重跑或回补

## 3. 字段覆盖（仅统计有内容样本）

- 样本范围: classify_record == ok 且 来源类型 ∈ {html, pdf}
- 核心字段 5 个: 职称 / 研究方向 / 教育经历 / 工作经历 / 联系方式
- 阈值: **平均覆盖率 ≥ 70%**（即 score 公式里 avg_coverage/0.7 封顶 1.0）
- 页面确实无某字段信息 → 留空合法，不算错

## 4. 输出契约

- **25 列**: 12 元数据（姓名/详情页URL/来源类型/原始文件/头像文件/has_pdf/
  pdf_url/pdf_path/html_path/采集时间/失败原因/重试次数）+ 13 字段
- `validate_spider_output` 校验: 列全 + 来源类型非空 + 有数据行
- 结果必须来自 spider.py 运行（可复现）
- 原始文件语义: 有 PDF 则原始文件=PDF 路径，否则=HTML 路径；
  文件名 `<姓名>_<url前8位md5>.<ext>`

## 5. 失败语义与标记

- `失败原因`: "" = 无失败；否则 "fetch: …(重试N次后仍失败)" /
  "pdf_download: …" / "LLM提取失败(超时/熔断)，已降级启发式"
- `重试次数`: 成功路径上的额外重试次数（0 = 一次成功）
- failed / pending 行在 RESUME=1 重跑时自动重试（不得跳过）

## 6. 质量校验（只报告，不扣分）

- 职称 ∈ 词表（FIELD_SPEC.values）
- 邮箱格式合法
- 占位值（备注/无/暂无）不得当字段值
- 页面有内容但字段全空 → 疑正文容器选错，列入问题

## 7. 评分公式与通过线

```
pdf_shell 且 DOWNLOAD_PDF=0（pending 模式）:
  score = 40×success_rate + 40×pdf_rate + 20×pdf_rate
其余:
  score = 40×success_rate + 40×min(avg_coverage/0.7, 1.0) + 20×pdf_rate
```

- **通过: score ≥ 70 且 success_rate ≥ 90%**
- 不通过 → 重新分析/重新生成/重跑（最多 3 轮）

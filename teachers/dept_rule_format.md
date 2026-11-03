# 按学院提取规则工作区（并发子代理产出）

每个子代理负责一个学院: 分析本地 raw HTML 模板 → 设计规则 → 离线验证 →
写 `dept_rules/<学校>_<学院>.json`。主循环合并进 detail_scraper._DEPT_EXTRACT_RULES。

规则 JSON 格式:
{
  "school": "北京大学", "dept": "政府管理学院",
  "templates": "模板描述",
  "rules": {"字段": ["正则1", "正则2"]},
  "offline_test": {"样本名": {"字段": "提取值"}},
  "notes": "注意事项"
}

# 学信网 — 全国高校招生信息

## 数据源

| 项目 | 详情 |
|------|------|
| **网址** | https://gaokao.chsi.com.cn/gdwz.do |
| **运营方** | 学信网 (CHSI / 教育部学生服务与素质发展中心) |
| **数据量** | 2,921 条 / 147 页 (每页 20 条) |

## 采集字段

| # | CSV列 | JSON字段 | 说明 |
|---|-------|----------|------|
| 1 | orgCode | `orgCode` | 院校代码 (5位) |
| 2 | 学校名称 | `orgName` | |
| 3 | 学校网址 | `xxwz` | 官网 URL |
| 4 | 招生网址 | `zswz` | 招生网 URL |
| 5 | 咨询电话 | `dh` | |
| 6 | 微信名称 | `wxmc` | 公众号名称 |
| 7 | 微信号 | `wxh` | 微信号 ID |
| 8 | 微博名称 | `wbmc` | |
| 9 | 微博网址 | `wbwz` | |
| 10 | 百家号名称 | `bjhmc` | |
| 11 | 百家号ID | `bjhIdh` | |
| 12 | 新媒体名称 | `zyckxtmc` | 其他新媒体平台名称 |
| 13 | 新媒体网址 | `zyckxtwz` | 其他新媒体平台链接 |

## 输出文件

| 文件 | 说明 |
|------|------|
| `chsi_colleges.csv` | 结构化数据 (UTF-8 BOM, 428 KB) |
| `chsi_gdwz_raw.jsonl` | 原始 JSON，每行一所学校 (2.0 MB) |
| `scrape_chsi.py` | 采集脚本 |
| `checkpoint_chsi.json` | 断点文件 |

## 反爬技术分析

### 瑞数 (Ruishu) 动态验证

学信网使用 **瑞数动态安全** 进行反爬保护，存在两个关键机制：

1. **动态 Cookie**: `goaYXsyEWlxdO` / `goaYXsyEWlxdP`
   - 首次访问时由服务端 JS 动态生成
   - 在浏览器环境执行 JS 运算后设置
   - 必须在同一 session 中携带

2. **加密参数 `p3LORH7S`**:
   - 随页面 URL 携带的加密 token
   - 由前端 JS 对页面表单数据动态运算生成
   - 每次请求值不同，绑定请求参数

### 绕过方案

**方案**: Playwright 浏览器自动化

```
浏览器启动 → 加载页面 → 执行瑞数 JS → 自动获取 cookie + p3LORH7S
                                                      ↓
                            在浏览器上下文中 fetch API (cookie 自动携带)
```

关键点：
- 使用真实 Chromium 浏览器 (headless)
- 在 `page.evaluate()` 中发起 `fetch` 请求，复用浏览器指纹和 cookie
- `credentials: 'include'` 确保 cookie 自动附加
- 不需要手动提取或计算 p3LORH7S

### 运行

```bash
# 确保已安装 Playwright
uv add playwright && uv run playwright install chromium

# 执行采集
cd chsi && uv run python scrape_chsi.py
```

### 注意事项

- Playwright 需要下载 Chromium (~150MB)，仅首次需要
- `headless=True` 模式运行，无需显示器
- 如果 cookie 过期 (session 超时)，脚本会重新打开页面获取新 cookie
- 每页请求间隔 200-500ms 随机延迟

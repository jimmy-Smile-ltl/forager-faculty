# 教育部全国普通高等学校名单

## 数据源

| 项目 | 详情 |
|------|------|
| **网址** | https://hudong.moe.gov.cn/qggxmd/ |
| **发布方** | 中华人民共和国教育部 |
| **发布时间** | 2023-06-19 |
| **数据量** | 2,952 条 / 148 页 (每页 20 条) |

## 采集字段

| # | 字段 | 说明 |
|---|------|------|
| 1 | 序号 | 1 - 2952 |
| 2 | 学校名称 | 官方全称 |
| 3 | 学校标识码 | 教育部统一编码 (10位) |
| 4 | 主管部门 | 教育部 / 省教育厅 / 中央部委 等 |
| 5 | 所在地 | 省份 + 城市 |
| 6 | 办学层次 | 本科 / 专科 |
| 7 | 备注 | 民办 / 中外合作办学 等标注 |

## 输出文件

| 文件 | 说明 |
|------|------|
| `全国普通高等学校名单.csv` | 结构化数据 (UTF-8 BOM, 221 KB) |
| `scrape_colleges.py` | 采集脚本 |
| `checkpoint.json` | 断点文件 |

## 技术方案

### API 接口

```
GET https://hudong.moe.gov.cn/school/wcmdata/getDataIndex.jsp?listid=10000101&page={n}&keyword=
```

返回 HTML `<tr>` 行，每行 7 个 `<td>`。

其他辅助接口：

| 接口 | 用途 |
|------|------|
| `getHeader.jsp?listid=10000101` | 表头 HTML |
| `getCounts.jsp?listid=10000101` | 总记录数 ("已为您检索出2952条结果") |
| `getPage.jsp?listid=10000101&page={n}` | 分页导航 |

### 反爬绕过

网站使用 **WebSAAS WAF** (阿里云盾)。直接 POST 请求返回 502。

- **方案**: `curl_cffi` 模拟 Chrome 131 浏览器指纹 (`impersonate="chrome131"`)
- **注意**: GET 方法可用，POST 被拦截

### 运行

```bash
cd moe_gov && uv run python scrape_colleges.py
```

### 特性

- 断点续爬: 中断后重新运行自动跳过已完成的页码
- 速率控制: 每页间隔 0.5-1.5 秒随机延迟
- 自动重试: 遇到 502/522 自动重试 3 次
- CSV append 模式: 每爬完一页即写入，不丢失数据

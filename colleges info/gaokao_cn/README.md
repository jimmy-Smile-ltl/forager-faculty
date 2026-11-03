# 掌上高考 — 全国高校信息

## 数据源

| 项目 | 详情 |
|------|------|
| **网址** | https://www.gaokao.cn/school/search |
| **运营方** | eol.cn (中国教育在线) |
| **数据量** | 2,991 所高校 |

## 采集字段

| # | 字段 | 说明 | 来源 |
|---|------|------|------|
| 1 | 校徽URL | `https://static-data.gaokao.cn/upload/logo/{id}.png` | 静态 CDN |
| 2 | 学校名称 | | 列表 API |
| 3 | 所在地 | 省份+城市 | 列表 API |
| 4 | 院校特色 | 985/211/双一流/C9… (顿号分隔) | 列表 API + info.json |
| 5 | 办学层次 | 本科 / 专科 | 列表 API |
| 6 | 院校类别 | 综合类/理工类/师范类… | 列表 API |
| 7 | 办学性质 | 公办/民办/中外合作… | 列表 API |
| 8 | 保研星级 | 1-5 星 (空=无保研资格) | info.json |
| 9 | 软科排名 | 软科中国大学排名 (500+ = 未进前500) | info.json |
| 10 | 校友会排名 | 校友会中国大学排名 | info.json |
| 11 | QS排名 | QS 中国大陆排名 | info.json |
| 12 | QS世界排名 | QS 世界大学排名 | info.json `rank.qs_world` |
| 13 | US News排名 | US News 世界大学排名 | info.json |
| 14 | 武书连排名 | 武书连大学排名 (多数为空) | info.json |
| 15 | 学科评估 | 第四轮学科评估各等级学科数 (JSON, 如 `{"A+":"21",...}`) | info.json |
| 16 | 详情页URL | `https://www.gaokao.cn/school/{id}` | 模板拼接 |

## 输出文件

| 文件 | 说明 |
|------|------|
| `gaokao_colleges.csv` | 结构化数据 (UTF-8 BOM, 16 列) |
| `scrape_gaokao.py` | 采集脚本 |
| `backfill_rankings.py` | 排名回填脚本 (为旧 CSV 补排名, 断点续跑) |
| `checkpoint_gaokao.json` | 断点文件 |
| `rank_backfill_checkpoint.json` | 排名回填断点文件 |

## 技术方案

### API 接口

**列表 API** (无鉴权):

```
GET https://api.eol.cn/gkcx/api/?
  uri=apidata/api/gk/school/lists
  &page=1
  &size=30
  &sort=view_total
  &request_type=1
```

返回 JSON: `data.item[]` 为学校列表, `data.numFound` 为总数。共 100 页。

**详情 JSON** (静态 CDN，无鉴权):

```
GET https://static-data.gaokao.cn/www/2.0/school/{school_id}/info.json
```

返回学校详细信息 (985/211 标签、保研星级、双一流学科列表等)。

### 字段编码映射

**办学层次** (`level`):
| 编码 | 含义 |
|------|------|
| 2001 | 本科 |
| 2002 | 专科 |

**院校类别** (`type`):
| 编码 | 含义 |
|------|------|
| 5000 | 综合类 |
| 5001 | 理工类 |
| 5002 | 农林类 |
| 5003 | 医药类 |
| 5004 | 师范类 |
| 5005 | 语言类 |
| 5006 | 财经类 |
| 5007 | 政法类 |
| 5008 | 体育类 |
| 5009 | 艺术类 |
| 5010 | 民族类 |
| 5011 | 军事类 |
| 5012 | 其他 |

**办学性质** (`nature`):
| 编码 | 含义 |
|------|------|
| 36000 | 公办 |
| 36001 | 民办 |
| 36002 | 中外合作办学 |
| 36003 | 内地与港澳台地区合作办学 |

**特色标签来源**:
- 列表 API 基础字段: `f985`, `f211`, `dual_class`, `department`, `admissions`, `doublehigh`
- info.json 扩展标签: `label_list` (C9、国防七子、建筑老八校、五院四系 等)、`is_military_academy`、`is_police_*`
- `recommend_master_level` → 保研星级

**排名来源** (info.json):
- 顶层字段: `ruanke_rank`(软科), `xyh_rank`(校友会), `qs_rank`(QS中国), `us_rank`(US News), `wsl_rank`(武书连)
- `rank.qs_world` → QS世界排名; `xueke_rank` → 学科评估 (各等级学科数)
- 0/缺失 = 未上榜, 输出为空串; 软科排名 `500+` 表示未进前 500

### 运行

```bash
cd gaokao_cn && uv run python scrape_gaokao.py     # 全新采集 (含排名)
cd gaokao_cn && uv run python backfill_rankings.py # 为旧 CSV 回填排名
```

### 特性

- 双数据源融合: 列表 API (快速) + 详情 JSON (丰富标签)
- 40 线程并发: 获取 2991 条详情约 2 分钟
- 断点续爬: `checkpoint_gaokao.json` 记录已完成 school_id
- 标签去重: 自动合并 强基/强基计划、双高/双高计划 等同义词

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 spider 的共享工具箱 — 所有通用逻辑集中于此, spider.py 只写站点特定选择器。

设计原则(2026-08-22 用户指令):
  生成的 spider 必须尽量简约; 网络请求/姓名清洗/输出写盘/分页枚举等通用能力
  一律复用本模块, 禁止 spider 内联实现 — 否则每个 spider 复制一份, 历史上
  smart_fetch 签名错、职称粘连、meta 溯源失配、分页缺失等 bug 会反复出现。

约定:
  spider.py 通过 sys.path 引导 import 本模块:
    import sys as _sys
    from pathlib import Path as _P
    _sys.path.insert(0, str(_P(__file__).resolve().parents[4]))
    from util.teacher_extract import NAV_WORDS, clean_name, extract_teachers, write_outputs, list_pages
"""
import csv
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

# ── 导航/栏目词表(排除名单): 全量, 不截断 — 截断曾漏掉"首页"(排序469)导致模板提取导航词 ──
NAV_WORDS = {
    "下载", "下页", "专业", "中心", "中文", "主任", "主页", "书记", "书院", "交流", "人才", "会议", "体操", "体育", "党委", "党建",
    "最前页", "最后页", "月至今", "主治", "北京",  # 2026-10-09 分页控件/职称/经历碎片实锤(石大环境/南医大苏北)
    "化工", "化学", "房间", "月至", "出版物", "环境", "工程",  # 2026-10-09 石大环境系经历/出版物页碎片实锤(LLM终审零容忍)
    "年出版", "获奖", "曾参加", "曾担任", "曾主持", "曾获得", "主持", "参与", "发表", "主编", "参编",  # 2026-10-09 叙述型简历页碎片(石大环境系实锤)
    "序号", "姓名", "性别", "年龄", "学历", "学位", "备注", "编号", "照片", "学制", "学分", "班级", "年级", "联系方式", "课程名称", "课程",  # 2026-10-09 表格表头词(石大企业管理系课程表实锤: 序号/姓名/学位/备注被当教师)
    "关于我们", "部门介绍", "联系我们", "人事招聘", "电子办公", "中国文学", "历史学系", "观察者网", "人民日报", "全部",  # 2026-10-10 复旦站点导航/栏目词实锤(体育教学部40个导航词/中国研究院)
    "委员", "会议名称", "报告人", "口头", "大会主席", "看全书", "贴图号", "服务号", "公众号", "购买", "会议", "学术报告", "报告",  # 2026-10-10 复旦机构站会议/推广碎片实锤(核物理实验室/人类表型组)
    "现有教授", "组织构成", "就业在线", "高校科技", "高校资讯", "数说高校", "关注两会", "人物热点",  # 2026-10-10 批次5碎片实锤(广西研究中心/合工大建艺/南邮通达门户)
    "相关网站",  # 2026-10-10 复旦中国社会主义市场经济研究中心页脚链接实锤
    "论文", "收录", "教材", "收录约", "余篇被", "发表论文", "专著",  # 2026-10-11 武理工物理论文发表碎片实锤
    "壹学者", "意见箱", "板球", "防身术", "健美操", "篮球", "足球", "排球", "羽毛球", "乒乓球", "网球", "武术", "田径", "游泳", "体操", "击剑", "柔道", "跆拳道", "散打", "龙舟", "赛艇", "高尔夫", "垒球", "棒球",  # 2026-10-11 北邮体育部课程项目被当教师实锤
    "公告", "其它", "出版", "创新", "前沿", "医师", "博导", "合作", "团委", "国际", "地址", "科学", "培养", "基地", "基金", "媒体", "学术",
    # 2026-10-24 医院科室词(常州一院名单"肿瘤学/核医学/妇科"、南医大一临
    # "护理概况/护理动态"被当教师实锤) — NAV_WORDS 是整词匹配, 真名不受影响
    "肿瘤学", "核医学", "妇科", "护理概况", "护理动态", "护理管理",
    "内科", "外科", "儿科", "骨科", "眼科", "口腔科", "皮肤科", "急诊科",
    "麻醉科", "检验科", "影像科", "放射科", "病理科", "药剂科", "康复科",
    "感染科", "妇产科", "耳鼻喉科", "重症医学科", "临床医学", "基础医学",
    "防护", "上页",  # 2026-10-25 批次2349页面控件词被当姓名实锤
    "学校", "学科", "学系", "学院", "实践", "导师", "导航", "就业", "尾页", "工会", "师资", "平台", "建设", "招生", "招聘", "搜索",
    "教学", "教师", "教授", "文化", "新闻", "时讯", "更多", "服务", "期刊", "末页", "机构", "查看", "校友", "概况", "注册", "电话",
    "登录", "硕导", "确定", "科研", "简介", "简章", "管理", "系所", "联系", "艺术", "行政", "视听", "讲师", "设置", "详情", "课程",
    "跳转", "返回", "通知", "邮箱", "邮编", "部门", "院士", "院长", "项目", "领导", "首页", "上一页", "下一页", "体育馆", "出版社",
    "副主任", "副教授", "准聘", "副院长", "办公室", "医学部", "博士后", "合作者", "图书馆", "在校生", "奖学金", "委员会", "学位点", "学生会",
    "实验室", "宣传片", "工程师", "教务处", "教务部", "教研室", "教职工", "新闻网", "本科生", "校友会", "研究员", "研究生", "系主任",
    "财务处", "运动会", "下载专区", "专业介绍", "专业信息", "专业设置", "专职教师", "两委委员", "两院院士", "个人主页", "中医临床", "中国传媒",
    "中心简介", "书记信箱", "交大校历", "交大简介", "交流合作", "人事系统", "人才培养", "人才建设", "人才引进", "人才招聘", "人才计划", "人才队伍",
    "介入医学", "仪器平台", "会议室预", "会议论坛", "会议通报", "作风建设", "信息公开", "党务工作", "党团学工", "党团工会", "党团建设", "党委书记",
    "党建动态", "党建工作", "党建思政", "党政班子", "党政管理", "党政领导", "党群工会", "党群机构", "党群建设", "全职教师", "公示信息", "关于我们", "关于本院",
    "兼职教授", "创建百年", "制度建设", "制度流程", "副研究员", "办事大厅", "办事指南", "办事流程", "办公服务", "加入我们", "勤工助学", "化学印迹",
    "化学教学", "北京大学", "北大邮箱", "北大首页", "博士导师", "历任领导", "历史沿革", "历届大专", "友情链接", "双聘教授", "发展历程", "合作交流",
    "合作需求", "同等学力", "品牌支部", "团队建设", "团队概况", "国家优青", "国家杰青", "国际交流", "在校学生", "培养信息", "培养工作", "培养方案",
    "培训进修", "基地平台", "基础医学", "基础理论", "复旦大学", "复旦新闻", "复旦校历", "复旦校友", "复旦简介", "媒体聚焦", "学习专区", "学习咨询",
    "学习园地", "学位工作", "学子风采", "学工动态", "学工队伍", "学术交流", "学术刊物", "学术动态", "学术团队", "学术成果", "学术报告", "学术期刊",
    "学术研究", "学术科研", "学术讲座", "学校主页", "学校公共", "学校官网", "学校标识", "学校概况", "学校领导", "学校首页", "学生交流", "学生党建",
    "学生工作", "学生社团", "学生风采", "学的风采", "学科介绍", "学科建设", "学科方向", "学系概况", "学者观点", "学部动态", "学部概况", "学部简介",
    "学部领导", "学院主页", "学院介绍", "学院优秀", "学院信箱", "学院历史", "学院总览", "学院概况", "学院简介", "学院领导", "学院首页", "实验中心",
    "实验教学", "客座教授", "宣传视频", "导师介绍", "导师风采", "导的风采", "就业信息", "工会风采", "工程技术", "工程硕士", "师德师风", "师资介绍",
    "师资力量", "师资建设", "师资概况", "师资简介", "师资队伍", "师资现状", "师资结构",
    "教师结构", "运动成绩", "师资队伍简介", "常用下载", "常用电话", "平台基地", "年度捐赠", "当前位置", "思想建设", "情暖教师",
    # 2026-09-16 北科工程技术研究院实锤: 工研院"技术方向"栏目词被当教师提取
    # (先进材料/工艺装备/智能检测/精准控制/智能制造) — 工科栏目词永不可能是人名
    "先进材料", "工艺装备", "智能检测", "精准控制", "智能制造", "技术方向",
    "现有资质", "政策之窗", "软著", "业绩总览", "品种开发", "业务范围", "获得荣誉", "工研影像",
    "成果奖励", "所获荣誉", "招生专业", "招生信息", "招生就业", "招生招聘", "招生系统", "招生问答", "招聘信息", "招贤纳士", "捐赠动态", "捐赠支持",
    "捐赠项目", "支部活动", "政治建设", "政策园地", "教务管理", "教学基地", "教学大纲", "教学教务", "教学督导", "教学管理", "教学通知", "教师主页",
    "教师交流", "教师介绍", "教师列表", "教师发展", "教师名单", "教师名录", "教师简介", "教师队伍", "教授登录", "教的风采", "教职员工", "教育捐赠",
    "教育教学", "教育管理", "教育部网", "数字媒体", "数据科学", "新闻中心", "新闻动态", "新闻资讯", "旧版网站", "科学·经", "新文科平", "机构知识", "时光剪影", "智慧安大", "智班培养",
    "智能校友", "服务动态", "服务平台", "服务流程", "服务社会", "未来学生", "未来学院", "未来科学", "本科招生", "本科教育", "本系概况", "本院概况",
    "机构设置", "校企合作", "校内资源", "校内链接", "校友之家", "校友动态", "校友名录", "校友导师", "校友捐赠", "校友资讯", "校友风采", "校史沿革",
    "校园新闻", "校园生活", "校园风物", "校长信箱", "横向科研", "活动报名", "版权所有", "特聘教授", "现任领导", "生理科学", "电子校友", "电子邮箱",
    "留学交换", "留学复旦", "病毒防治", "直属单位", "相关链接", "知名专家", "知名校友", "研的风采", "研究团队", "研究平台", "研究成果", "研究方向",
    "研究机构", "研究生培", "研究生招", "硕士导师", "社会服务", "社会兼职", "科学研究", "科技概况", "科技获奖", "科研人员", "科研动态", "科研基地", "科研工作", "常任", "常任轨", "常任教授", "兼职", "在职", "专任",
    "科研平台", "科研成果", "科研活动", "科研训练", "科研论文", "管理入口", "管理技术", "管的风采", "系委员会", "纪律建设", "纵向科研", "组织建设",
    "组织机构", "组织架构", "组织结构", "统计数据", "继续教育", "网关登录", "网关登陆", "网站首页", "网络教学", "考生与访", "职业发展", "联系我们",
    "联系电话", "育的风采", "苏大党委", "苏大学生", "苏大教务", "苏大网关", "荣休教师", "荣誉教授", "药学管理", "行业新闻", "行政人员", "行政办公",
    "行政教辅", "行政管理", "行政部门", "表格下载", "规章制度", "视觉传达", "讲座教授", "论文系统", "访问学者", "评估系统", "语言中心", "课程信息",
    "课程设置", "课题研究", "资料下载", "超声医学", "跨媒体通", "运动场馆", "返回首页", "远程教育", "通知公告", "通知发文", "重点学科", "阅读专区",
    "队伍建设", "队伍概况", "附属单位", "院内信息", "院内门户", "院友服务", "院士风采", "院系设置", "院长信箱", "院长致词", "院长致辞", "青年人才",
    "青年论坛", "面试系统", "领导信箱", "领导班子", "领导简介", "Email", "企业家课程", "党委副书记", "博士后培养", "博士后招聘", "在职研究生",
    "客座研究员", "本科生培养", "本科生教育", "留学生教育", "研究生培养", "研究生导师", "研究生招生", "研究生教育", "重点实验室", "创新港研究院",
    "国际教育学院", "政企定制课程", "校园基本建设", "校园通讯建设", "钱学森图书馆", "ENGLISH", "English", "光子传输与通信", "档案馆、博物馆",
    "西交利物浦大学", "信息与通信研究所", "应用电子学研究所", "物理电子学研究所", "研究中心和实验室", "量子电子学研究所", "碳基电子学研究中心",
    "纳米器件物理与化学",
    # ── 学历/成员类栏目词(2026-08-22 批次15复旦研究中心 FAIL 沉淀):
    # 博士/硕士/学士 是招生栏目导航, 成员/职责/小组 是研究中心页栏目 —
    # 通用词直接硬编码, 任何学校第一次遇到即排除, 不必等进化词表第二次
    "博士", "硕士", "学士", "成员", "职责", "小组", "成员简介", "主要成员", "学术小组",
    # ── 2026-08-27 四轮: 兼职/挂职/办公/实习 整词(栏目) — 社会兼职/院内办公 FAIL 沉淀 ──
    "兼职", "挂职", "办公", "实习",
    # ── 2026-08-27 五轮: 人才科 等机构科室名(3字, "科"不能入 GLUE/NAV 后缀 —
    #    "李科/王文科" 是真名, 尾词法会误杀, 只能整词精确拒) ──
    "人才科",
    # ── 2026-08-28 六轮: 媒体/栏目类整词 — 宣传片同族 形象片/示教片,
    #    专家采访(专家+采访) 04:50 批次 FAIL 沉淀 (讲坛/讲堂 已入 NAV_SUFFIX 尾词) ──
    "形象片", "示教片", "专家采访",
    # ── 2026-10-14 医院科室名(中山/泰州人工分析实锤): 2字科室名精确拦截 —
    # 词表精确匹配不误杀真名("马科"不在表放行), 3-4字XX科由 merge 层规则拦
    "病理科", "急诊科", "口腔科", "超声科", "放射科", "检验科", "药剂科",
    "麻醉科", "儿科", "眼科", "牙科", "骨科", "皮肤科", "耳鼻喉科", "影像科",
    # 2026-10-14 中山医院补充(128人8噪声实锤): 心内科/妇产科/输血科等
    "心内科", "妇产科", "输血科", "疼痛科", "胸外科", "血液科", "神经内科",
    "神经外科", "泌尿外科", "普外科", "两院", "历任",
    "康复科", "理疗科", "营养科", "体检科", "门诊部", "住院部", "重症医学科",
    "员工邮箱",
}

# ── 真名保护名单 (2026-08-27 23:25 定案) ──
# fix_loop 把评估器误判的获奖/成果列表"作者栏"真名自动沉淀进进化词表
# (knowledge_nav_words.json), 全站永久误杀。光删词表无效 — 评估器每次复评
# 旧 CSV 都会把同名再沉淀回来 (实测 李建军/李鑫 21:24 删除后 23:25 复现)。
# 硬编码已确认真名并在进化词表合并后剔除出 NAV_WORDS, 8 处 nav_words 检查
# 天然免疫; clean_name 另加最优先放行作双保险。
# 安全边界: 均为 2-4 字常见人名, 不命中 GLUE/NAV_SUFFIX/SUBJECT 任何规则;
# 误放行的代价(个别栏目词漏过滤) << 误杀的代价(全站缺失该教师)。
REAL_NAME_PROTECT = {
    "李建军", "李强", "李鑫", "王怡", "李季", "齐景坤",
    # 2026-09-24 复旦数学实锤: 张静(教授/博导, 带详情URL)被LLM终审误判非人名
    # 2026-09-24 中大管院同型: 张斌(带详情URL)LLM误判
    "张静", "张斌", "张玲", "吴琼", "张宁", "王立新",
    # 2026-09-30 东北农大化学实锤: 杨柳(带详情URL, 石河子外语同型)被LLM终审
    # 误判非人名 → 零容忍整院 FAIL — 常见名保护
    "杨柳",
    # 2026-10-01 北大新材料实锤: 张宏(带详情URL)被LLM终审误判 — 常见名保护
    "张宏",
    # 2026-10-02 北理工生命实锤: 张莹(带详情URL)被LLM终审误判 — 常见名保护
    "张莹",
    # 2026-08-28 07:20: 陈智恒宇 — 4字真名(陈+三字名, NAME_RE 命中, 不撞任何规则),
    # 被评估器误判沉淀进进化词表 → 全站拒绝 (与李建军同型污染)
    "陈智恒宇",
    # 2026-09-03: 李大字 — 北化大信息学院真人(详情页 <title>李大字</title>,
    # c15683a220250/page.psp), LLM 终审误判"截断词"→沉淀进进化词表→确定性
    # NAV_WORDS 全站误杀。与李建军同型污染, 硬编码保护后免疫复沉淀。
    "李大字",
    # 2026-09-04: 准聘粘连污染 — 中南大学卓越工程师学院页 "王鑫准聘教授" 被
    # 剥"教授"后剩"王鑫准聘"(准聘职称词缺位) → 评估器误判非名 → 沉淀进化词表
    # (王鑫准聘/赵波准聘/聂阳准聘/施睿准聘/赖睿准聘/房旭准聘/安妮准聘/张鹏准聘)。
    # 词表已清, TITLE_SUFFIXES/NAV_WORDS 已补准聘; 真名入保护名单免疫复沉淀。
    "王鑫", "赵波", "聂阳", "施睿", "赖睿", "房旭", "安妮", "张鹏",
    # 2026-10-09 全库 fail_extraction 终审误杀扫描(979例归因): 常见名被 LLM
    # 终审零容忍误杀整院 — 陈鹏9次/吴静8/杨凯7/杨浩7/刘翠7/张帅7 等,
    # 均无详情URL孤词形态(带URL的已有拼音URL豁免, 这批是纯文本名单孤词)。
    # 人工复核通过(全部为高频真实姓名), 入保护名单免疫终审+词表再污染。
    "陈鹏", "吴静", "杨凯", "杨浩", "刘翠", "张帅", "张颖", "杨勇",
    "张明", "王安琪", "王涛", "李晶", "李俊", "张涛", "王勇", "徐海峰",
    "陈洁", "赵丹", "李建华", "刘敏", "李旭", "王征", "李刚", "梁爽",
    "李琳", "李艳红", "赵洋", "张欣", "王腾", "杨立君",
    # 2026-10-09 中南财大统计数学实锤: 张婧/徐娟 真名被沉淀进进化词表
    # (knowledge_nav_words.json) 全站误杀 — 词表已剔除+保护名单免疫复发
    "张婧", "徐娟",
    # 2026-10-25 贸大外语学院实锤: 白玫BARZEGAR 外教名(中文名+拉丁姓连写)
    # 被LLM终审零容忍当非人名整院FAIL(90人) — 真人保护免疫终审
    "白玫BARZEGAR",
    "马楠长聘",  # 2026-10-25 北大人文学部实锤: 真名+长聘粘连被LLM误判沉淀进化词表
    "金日成",  # 2026-10-26 疑似朝鲜族真名被LLM终审误判(保护, 复核后移除)
    # 2026-10-25 瑞数批(1025_0215)零容忍误杀实锤: 赵一鸣(北理工前沿交叉95人
    # 整院FAIL)/何燕李(川大文学院62人)/朱䒟(华科未来266人) — 均为名单真名
    # 带真实详情URL, LLM终审误判非人名 → 保护名单免疫
    "赵一鸣", "何燕李", "朱䒟",
    # 2026-10-09 100院系批量测试实锤: 吴萌/王婧/丁辉/张鑫(南医大苏北/
    # 石大北京)与王鑫帆/张在雄/田福淑/申师明/洪文杰(疑似朝鲜族名)被
    # LLM终审误判沉淀进词表全站误杀 — 词表大清洗(5878条)+守卫后补充保护
    "吴萌", "王婧", "丁辉", "张鑫", "王鑫帆", "张在雄", "田福淑", "申师明", "洪文杰",
    # 2026-10-10 南邮社会人口学院纯文本孤词名单实锤: LLM终审批量误杀32个真名
    # (冯加严/凌珑/刘亚群/张航/沙莎/王钧天/锋竹沁... 全部3字真名) — 保护名单
    "冯加严", "凌珑", "刘亚群", "刘璐婵", "刘筱勤", "张航", "张雯熹", "徐孝婷",
    "李发志", "李芷莙", "杨璐", "林宁", "沙莎", "熊文明", "王钧天", "王雪芹",
    "胡高强", "范兆媛", "解晓娜", "锋竹沁", "陈振", "陈灿君",
}

# ── 词表自我进化: fix_loop 每轮把评估器捕获的非名沉淀到 college teacher/
#    knowledge_nav_words.json, 本模块加载时合并 — 站点专名(苏迪科技/维程互联)
#    第二次遇到即被排除, 不必等 LLM 修复轮次 ──
_EVOLVE_PATH = Path(__file__).resolve().parent.parent / "knowledge_nav_words.json"
try:
    _evolve = json.load(open(_EVOLVE_PATH, encoding="utf-8"))
    if isinstance(_evolve, list):
        NAV_WORDS |= set(_evolve)
except Exception:
    pass
# 真名保护: 进化词表污染的已确认真名从 NAV_WORDS 剔除 (评估器误判沉淀回也免疫)
NAV_WORDS -= REAL_NAME_PROTECT

# ── 职称/职务粘连词(长词优先, 尾剥): "余臻职称"→余臻 "江娇党委"→江娇 ──
# 单字"研""党"风险高不加: 3字名尾字可能真为研/党
# 2026-08-27 补"副主任"(排"主任"前): "王副主任"原被"主任"先剥成"王副"误当人名
TITLE_SUFFIXES = ["教授级高级工程师", "高级工程师", "博士生导师", "硕士生导师",
                  "副主任医师", "主任医师", "主治医师", "住院医师",
                  "主任护师", "副主任护师", "主管护师",
                  "主任药师", "副主任药师", "主管药师",
                  "主任技师", "副主任技师", "主管技师",
                  "医学博士", "医学硕士",
                  "特聘教授", "副研究员", "助理研究员", "研究员", "助理教授",
                  "长聘副教授", "长聘教授", "长聘", "准聘副教授", "准聘教授", "准聘", "副教授", "教授", "讲师", "博导", "硕导",
                  "院士", "博士", "硕士", "副主任", "副院长", "党委", "党总支",
                  "职称", "院长", "副院", "主任", "医师", "书记", "党委",
                  "助理", "副", "研究",
                  "青年研究员", "青年副研究员", "青年"]  # 2026-10-10 复旦人类表型组实锤: 黄刚青年←青年研究员截断

NAME_RE = re.compile(r"^[一-鿿·]{2,4}$")

# 2026-10-13 机构页栏目行过滤(通用层) — 与 evaluate_teachers 的
# DETAIL_HINT/ORG_HINT 同口径: 栏目/列表/登录等ORG特征URL不落教师,
# DETAIL优先(教师详情即使含list特征也保留)。LLM spider 与模板薄壳
# 都经 extract_teachers, 此处为唯一共同落点(1522批89FAIL根修)
_DETAIL_URL_RE = re.compile(
    r"/info/\d+|shi\.buaa|teachershouw|/jszy/|/c[0-9a-f]{6,}/|"
    r"/\d{4,}/[a-z0-9]+/index\.htm|/\d{6,}\.htm|/[a-z0-9]{20,}\.htm|"
    r"/\d+/\d+\.htm|/[a-z]{1,3}/\d+\.htm|"
    r"t\d{8}_\d+\.shtml|"
    r"/ykzj/[a-z0-9]+/\d{4}_\d+_\d+\.html|"
    r"/[a-z-]+doctors-\d+\.html|expert\d+-\d+\.html|"
    r"/web\\.[^/]+/[a-z]{4,}/?|"
    r"faculty\.[^/]+/[a-z0-9]+/zh_CN/index\.htm|/[a-z]{2,}/[a-z]{2,4}/index\.htm|/professor/[a-z]+/?|/professor/[a-z]+/index\.html?|faculty\.[^/]+/\d+/|grzy\.[^/]+/[A-Za-z]+/zh_CN/", re.I)
_ORG_URL_RE = re.compile(
    r"/jgjj/|/szdwjj|/gzzz|/list\.htm|/index\.htm$|"
    r"login|enroll|thesis|questionnaire|interview|oauth|passport|/$", re.I)

# 姓名+空白+职称 粘连拆分用职称词表(长词优先: 正高级工程师 > 工程师)
# 2026-10-24 医院职称词(重大中心医院 _kszj 页实锤: <a>内 strong姓名+u职称,
# 锚文本"万晓强主任医师"粘连) — 长词在前(副主任医师 > 主任医师 > 医师),
# 高校页无医师类词不受影响
_TITLE_GLUE_PAT = (r"正高级工程师|教授级高级工程师|高级工程师|正高级实验师|"
                   r"高级实验师|教学副教授|教学助理教授|"
                   r"副主任医师|主任医师|主治医师|住院医师|医学博士|医学硕士|"
                   r"主任护师|副主任护师|主管护师|主任药师|副主任药师|主管药师|"
                   r"主任技师|副主任技师|主管技师|"
                   r"副教授|助理教授|副研究员|研究员|"
                   r"教授|讲师|助教|工程师|医师|护师|药师|技师|院士|博士后|博导|硕导|"
                   r"正高级|副高级|中级")

# 2026-09-23 央财政府管理: 嵌套子元素姓名扫描的「长词切分守卫」判定
# 相邻 span 是否为职称词 — 职称span(教授 硕导)是姓名+职称对, 不是长词
# 切分片段(长词片段形态是"为硕"+"士研究"+"生"这种无职称词的拼接)
_FRAG_TITLE_HINT = re.compile(
    r"^(?:正高级工程师|教授级高级工程师|高级工程师|高级实验师|"
    r"教学副教授|教学助理教授|副教授|助理教授|副研究员|研究员|"
    # 2026-10-24 医院职称span(<u>主任医师</u>)是姓名+职称对不是长词片段
    r"副主任医师|主任医师|主治医师|住院医师|医师|护师|药师|技师|"
    r"教授|讲师|助教|工程师|院士|博士后|博导|硕导|正高级|副高级|中级)")

# ── 外文名形态（2026-09-15 上科大实锤: 7 位外籍教师全漏）──
# "Assistant Professor Brett Goodin" / "Associate Professor Igor Fed..." /
# "Visiting Assistant Professor XXX" — 英文职称词前缀 + 2-4 词 Title Case 人名。
# 中文正则 ^[一-鿿·]{2,4}$ 是硬边界, 英文名必须独立分支, 不走 clean_name。
_ENG_TITLE_WORDS = {
    "associate", "assistant", "visiting", "adjunct", "full", "teaching",
    "research", "distinguished", "emeritus", "chair", "honorary", "guest",
    "postdoctoral", "postdoc", "professor", "lecturer", "instructor",
    "researcher", "fellow", "scientist", "scholar", "director", "dean",
    "dr", "prof", "mr", "ms", "mrs",
}
# 英文导航/栏目词 — 单个 Title Case 词不会误判(人名须 ≥2 词), 防的是
# "Read More"/"Contact Us"/"Faculty Members"/"Home Page" 双词形态
_ENG_NAV_WORDS = {
    "english", "faculty", "people", "home", "about", "contact", "research",
    "news", "events", "admissions", "academics", "apply", "login", "register",
    "more", "view", "read", "download", "search", "staff", "administration",
    "international", "program", "programs", "center", "centers", "office",
    "department", "school", "university", "directory", "members", "team",
    "us", "page", "site", "map", "academic", "study", "studies", "resources",
    # 2026-09-15 上外英语学院实锤: "SISU Global" 英文导航被当教师(2词Title Case)
    "global", "chinese", "version", "campus", "china", "education", "students",
    "alumni", "undergraduate", "graduate", "postgraduate",
}


def _valid_eng_name(name_words):
    """人名段校验: 2-4 词、无导航词、首尾词 Title Case(中间允许 de/van/von)。
    末词若是职称词=姓名+职称粘连(如 Igor Fedyukin Associate Professor),
    拒掉让后缀职称循环接手拆分 — 姓氏恰好撞职称词(Dean/Director 等)的
    教师会漏提, 概率极低, 接受此权衡。"""
    if not 2 <= len(name_words) <= 4:
        return False
    if any(w.lower() in _ENG_NAV_WORDS for w in name_words):
        return False
    if name_words[-1].lower() in _ENG_TITLE_WORDS or name_words[-1].lower().endswith("professor"):
        return False
    if not re.fullmatch(r"[A-Z][a-zA-Z'\-.]*", name_words[0]):
        return False
    if not re.fullmatch(r"[A-Z][a-zA-Z'\-.]*", name_words[-1]):
        return False
    for w in name_words[1:-1]:
        if not re.fullmatch(r"[A-Za-z][a-zA-Z'\-.]*", w):
            return False
    return True


def try_english_teacher(t: str):
    """'Assistant Professor Brett Goodin' / 'Igor Fedyukin Associate Professor'
    -> (title, 'Brett Goodin')。英文职称词可前可后; 不满足返回 None。"""
    words = t.split()
    if not 2 <= len(words) <= 7:
        return None
    # 前缀职称形态: Associate Professor Igor Fed...
    ti = 0
    while ti < len(words) and (words[ti].lower() in _ENG_TITLE_WORDS
                               or words[ti].lower().endswith("professor")):
        ti += 1
    if _valid_eng_name(words[ti:]):
        return (" ".join(words[:ti]), " ".join(words[ti:]))
    # 后缀职称形态: Igor Fedyukin Associate Professor (2026-09-15 上科大 jxjs 实测)
    tj = len(words)
    while tj > 1 and (words[tj - 1].lower() in _ENG_TITLE_WORDS
                      or words[tj - 1].lower().endswith("professor")):
        tj -= 1
    if tj < len(words) and _valid_eng_name(words[:tj]):
        return (" ".join(words[tj:]), " ".join(words[:tj]))
    return None

# 强导航尾词: 链接文本以这些词结尾的整条排除 — "兰大首页"/"本站首页"/"新文科平台"/
# "机构知识库" 等校名/栏目组合词曾被当教师姓名(兰大批次 7 个非人名的主因)
# 2026-08-27 强化: 机构/栏目单字与双字后缀 — "体育系"/"基础部"/"思政部"(院系部处馆室站组),
# "宣教园地"/"中师培训"/"书院"(栏目) 等 2-4 字组合全在提取层拒掉(评估器同源引用)。
# 安全边界: 真名不以 系|部|处|馆|室|站|组|所 结尾, 误杀率≈0。
NAV_SUFFIX_RE = re.compile(
    r"(首页|主页|平台|知识库|数据库|系统|门户|概况|简介|新闻|通知|公告|动态|下载|"
    r"搜索|登录|注册|导航|链接|中心|学院|大学|学校|图书馆|English|ENGLISH|"
    r"系|部|处|馆|室|站|组|所|园地|书院|培训|基地|工作室|教研室|实验室|"
    r"委员会|办公室|专栏|专区|兼职|挂职|办公|实习|夜校|讲坛|讲堂)$")

# 专业方向/栏目词(整词即非人名) — 与 evaluate_teachers.SUBJECT_HINT 同口径,
# 下沉到 spider 层直接拒绝: "本科教学/专业建设/教学培养" 类栏目词不再进CSV
# 2026-08-27 强化: 动画(动画系/动画专业栏目)/体育(体育系部)/思政(思政部/思政课程)
# 等学科专业词整词拒绝; 组合词("环境设计"=环境+设计)由 GLUE_RE 拦。
SUBJECT_RE = re.compile(
    r"(指挥|理论|作曲|音乐|舞蹈|美术|设计|摄影|表演|导演|编剧|钢琴|声乐|器乐|"
    r"管理|教育|研究|科学|工程|技术|学科|专业|方向|教学|实验|实践|基础|临床|"
    r"医学|药学|法学|经济|金融|会计|市场|新闻|传播|广告|文学|历史|哲学|"
    r"建设|培养|课程|文档|详情|第一页|教育|动画|体育|思政|党建|团委|工会|"
    r"培训|招生|就业|公告|活动|动态|专区|园地|书院|兼职|挂职|办公|实习)$")

# 名+栏目词粘连: "车某技术"/"王某教学" — 2-3字名 + 栏目词后缀(截断)
# 2026-08-27 强化: 补 设计|动画|培训|园地|书院|体育|思政|系|部|处|馆|室|站|组 —
# "环境设计"(环境+设计)曾被当教师名(栏目词整词匹配漏网)。
# 二轮补: 指挥|理论|人才 — "合唱指挥"/"音乐理论"/"党委人才" 4字栏目词(整词
# 不在 SUBJECT_RE, 前缀2字+后缀2字命中本模式); "X指挥/理论/人才"作人名不存在, 安全。
# 三轮补: 进展|设置|基金|计划|队伍|名录 — "科技进展"/"岗位设置"/"科学基金"/
# "人才计划"/"教师队伍"/"教师名录" 栏目组合词(上海中医药 FAIL 沉淀)。
# 四轮补: 兼职|挂职|办公|实习 — "社会兼职"/"院内办公"/"学术挂职"/"毕业实习"
# (18:27 批次 社会兼职/院内办公 FAIL 沉淀)。
# 五轮补: 专业|简史|名作|教辅|风光|高考|语音|全文|风采|专班|国美|发文 —
# "硕博专业"/"学院简史"/"名家名作"/"党政教辅"/"校园风光"/"阳光高考"/
# "科大语音"/"查看全文"/"博导风采"/"工作专班"/"视觉国美"/"人事发文"
# (21:20 批次 FAIL 沉淀, 全为栏目/机构/平台名, 无真名以这些词结尾)。
GLUE_RE = re.compile(
    r"^([一-龥·]{2,3})(技术|医学|科学|教学|研究|中心|学院|管理|工程|基地|平台|"
    r"培养|教育|服务|建设|职称|部门|办公室|详情|设计|动画|培训|园地|书院|"
    r"体育|思政|党建|团委|工会|招生|就业|新闻|公告|活动|动态|"
    r"指挥|理论|人才|进展|设置|基金|计划|队伍|名录|兼职|挂职|办公|实习|"
    r"专业|简史|名作|教辅|风光|高考|语音|全文|风采|专班|国美|发文|"
    r"系|部|处|馆|室|站|组|所)$")


_TITLE_BUCKET_WORDS = {"正高", "副高", "初中级", "初级", "中级", "高级",
                       "院士", "无", "暂无", "无职称", "其他", "职称",
                       "教授名录", "副教授名录", "讲师名录",
                       # 2026-09-04: 准聘/长聘 职称档列头(进化词表污染清理后
                       # 回补为字面词, 防列头整词漏滤被当姓名)
                       "准聘长聘"}


def clean_name(text: str) -> str:
    """从链接文本提取姓名: 剥括号/空白/职称粘连词, 2-4字中文名, 否则空串。
    栏目词(SUBJECT/GLUE)与导航尾词在 extract_teachers 原文层已拒, 此处兜底
    (2026-08-27 补齐: GLUE/NAV_SUFFIX 原先只在提取层查, 表格/卡片/JS数组/
    纯文本回退调用 clean_name 的路径漏检 — 体育系/宣教园地/环境设计混入根因)。"""
    t = re.sub(r"[（(].*?[）)]", "", text or "")
    t = t.replace(" ", "").replace("　", "").replace("\n", "").replace("\t", "")
    # 2026-10-09 职称前缀粘连(辽大药学院实锤: 「教授刘宇」整串被当姓名) —
    # 前置剥离: 「教授刘宇」整串已被进化词表 NAV_WORDS 收录, 后置剥离
    # 会被 NAV_WORDS 早退吞掉, 必须在词表检查前剥出真名
    t = re.sub(r"^(教授|副教授|讲师|助教|博士|博士生导师|院士)", "", t)
    # 真名保护名单最优先放行 (进化词表污染免疫, 2026-08-27 23:25)
    if t in REAL_NAME_PROTECT:
        return t
    # 机构/栏目尾词: "体育系"/"宣教园地"/"实验中心" → 非人名
    if NAV_SUFFIX_RE.search(t):
        return ""
    # 名+栏目词粘连: "环境设计"(环境+设计)/"中师培训"(中师+培训) → 非人名
    if GLUE_RE.match(t):
        return ""
    # 整词是专业/栏目词 → 非人名
    if SUBJECT_RE.match(t) and len(t) <= 4:
        return ""
    # 整词是导航/栏目词表精确词 → 非人名 (人才科 等 3 字机构科室,
    # "科"尾词法会误杀 李科/王文科 真名, 只能整词精确拒; 2026-08-27 五轮)
    if t in NAV_WORDS:
        return ""
    # 整词是职称档词(万鹏wp3 名单分类入口: 正高/副高/初中级/院士 等,
    # 复旦 ai 学院静态页分类链接被当姓名 2026-08-28 教训) → 非人名
    if t in _TITLE_BUCKET_WORDS:
        return ""
    for suf in TITLE_SUFFIXES:
        # len(t) > len(suf): 剥后至少留 1 字; 剥剩 1 字时 NAME_RE 会拒(需2-4字)。
        # 原条件 len(t) > len(suf)+1 导致"王副主任"(4字)不满足剥"副主任"(3字),
        # 退化剥更短的"主任"留下假名"王副"(2026-08-27 修)。
        if t.endswith(suf) and len(t) > len(suf):
            t = t[:-len(suf)]
            break
    # 2026-10-10 链式剥「青年」: 「李轶青年研究员」剥「青年研究员」后剩
    # 「李轶青年」(循环只剥一级) — 再剥「青年」尾(截断粘连根治)
    t = re.sub(r"青年$", "", t)
    m = NAME_RE.match(t)
    return m.group(0) if m else ""


# ── 教师列表容器定位 (2026-09-15 用户指令: "匹配规则应该是严格css选择器优先,
# 正则经常会错误匹配") — 先按 DOM 结构定位教师列表容器, 容器内链接才提取;
# 正则降级为容器内姓名形态校验, 不再全页裸扫 <a> ──
# 语义类名选择器(强信号, 第一优先): 教师列表页惯用容器类名, 可积累
_TEACHER_CONTAINER_SELS = [
    # 2026-10-02 上交机动力实锤: AJAX 名单按系/所分 66 个同名容器
    # (div.name-list.name-list2), 语义表缺该类名 → 走密度路径 [:3] 截断
    # 495 人只提 41。name-list 是教师列表强信号类名, 入语义表
    "div.name-list", "ul.name-list", "li.name-list",
    "ul.wp_list", "div.wp_list", "ul.teacher_list", "div.teacher_list",
    "ul.teacherlist", "div.teacherlist", "div.faculty-list", "ul.faculty",
    "div.js_list", "ul.js_list", "div.szdw", "div.szdw_list", "div.teachers",
    "ul.teachers", "div.list_teacher", "ul.list_teacher", "div.teacher-wrap",
    "ul.rymd", "div.rymd", "div.jsml", "ul.jsml", "ul.teacher-content",
    # 2026-09-16 北师大哲学实锤: 连字符变体 div.teacher-list(卡片容器) —
    # 语义选择器只有下划线版, 密度启发式又因 <a> 文本是"姓名+研究所"混合串
    # 不命中 → 容器漏定位, 回退全页扫描把导航词当教师(教研获奖/活动快讯)
    "div.teacher-list", "ul.teacher-list", "div.teacher-list-box",
    "div.teacher-list-box > div.teacher-list",
    # 2026-10-24 医院专题(重大中心医院 _kszj 页实锤): 医院专家列表惯用
    # 容器 person-list/expert-info/team-list — 高校页面无此类名不受影响,
    # 排表尾: 高校语义选择器优先命中
    "ul.person-list", "div.person-list", "li.expert-info", "div.expert-info",
    "ul.expert-list", "div.expert-list", "ul.team-list", "div.team-list",
]


def _looks_teacher_anchor(t: str) -> bool:
    """教师条目锚文本形态: 纯中文2-4字 / 姓名+空白+职称粘连 / 英文名形态。
    新闻标题(>6字)/栏目词(含导航尾词)不命中 — 密度启发式的打分依据。"""
    if not t or len(t) > 40:
        return False
    tt = t.strip()
    if re.fullmatch(r"[一-鿿·]{2,4}", tt):
        return not (NAV_SUFFIX_RE.search(tt) or tt in NAV_WORDS)
    if re.match(r"^[一-鿿·]{2,4}\s+", tt) and re.search(
            r"教授|讲师|助教|研究员|博士后|工程师|博导|硕导", tt):
        return True
    return try_english_teacher(tt) is not None


def _looks_teacher_anchor_el(a):
    """元素级教师锚判定 = 文本形态 + 卡片形态。
    2026-09-28 川大艺术 VSB 实锤: 名单包在 div.nav.clearfix 里(误导性类名),
    锚文本"黄宗贤美术学系"(姓名+单位粘连6字)过不了纯文本判定 → 守卫失败 →
    名单容器被导航剥离整块误杀。卡片形态: 子元素 class=name|unit 且文本纯
    中文2-4字非导航词(姓名+单位分离卡, 川大艺术 VSB p.name/p.unit)。"""
    if _looks_teacher_anchor(a.get_text(strip=True)):
        return True
    _nc = a.find(["p", "span", "div"], class_=re.compile(r"(?:^|\s)(?:name|unit)(?:\s|$)"))
    if _nc is not None:
        _nct = _nc.get_text(strip=True)
        if re.fullmatch(r"[一-鿿·]{2,4}", _nct) and _nct not in NAV_WORDS:
            return True
    return False


def _locate_teacher_containers(soup):
    """CSS 选择器优先定位教师列表容器:
    1) 语义类名选择器(教师列表惯用类名) — 强信号直接命中
    2) 教师链接密度启发式: 容器内教师形态链接 ≥4 且占比 ≥50%(新闻列表
       锚文本是长标题, 不满足形态 → 不会被选中)
    返回最深命中容器(去父容器重复); 找不到返回 [] (调用方回退全页扫描)。"""
    for sel in _TEACHER_CONTAINER_SELS:
        els = soup.select(sel)
        if els:
            # 2026-10-02 上交机动力实锤: AJAX 名单按系/所分 30+ 个同名容器
            # (div.name-list), 旧 [:3] 只取前3个 → 495人名单只提取41人。
            # 语义类名是教师列表强信号, 全量返回(提取侧去重兜底)
            return els[:40]
    hits = []
    for el in soup.find_all(["div", "ul", "ol", "section", "table", "tbody"]):
        links = el.find_all("a", href=True)
        if len(links) < 4:
            continue
        n_t = sum(1 for a in links if _looks_teacher_anchor_el(a))
        if n_t >= 4 and n_t / len(links) >= 0.5:
            hits.append(el)
    # 只保留最深容器: 父容器(el 是其他命中容器的祖先)去掉, 防同一列表重复计数
    deepest = [el for el in hits
               if not any(any(p is el for p in other.parents) for other in hits)]
    return deepest[:40]  # 2026-10-02 上交机动力实锤: 多分组名单容器 [:3] 截断漏采


_NAV_STRIP_SELS = ["nav", ".nav", "#nav", ".menu", "#menu", ".header", "#header",
                   ".footer", "#footer", ".sidebar", ".col_menu", ".wp-panel",
                   ".sub-menu", ".site-lang", ".breadcrumb", ".crumbs", ".path"]


def _strip_nav_guarded(soup):
    """导航/页脚容器剥离 — 带教师信号守卫(2026-09-18 东林材料 clhx.htm 实锤:
    站点把整个内容区包在 div.header.clearboth 里, 导航剥离先 decompose 掉教师
    列表 → 容器定位返回0 → 回退全页扫描时教师锚点已不在DOM → 全站0提取)。
    守卫: 容器内含≥2个教师形态锚(2-4字中文名+数字详情路径)时不剥离 —
    它不是导航, 是教师列表(非标准 class 名)。导航链接是语义路径(无数字段),
    数字详情路径是教师锚的铁信号, 双条件齐发才豁免。"""
    for _sel in _NAV_STRIP_SELS:
        for el in soup.select(_sel):
            _tl = [a for a in el.find_all("a", href=True)
                   if _looks_teacher_anchor_el(a)
                   and re.search(r"/\d+", a.get("href", "") or "")]
            if len(_tl) >= 2:
                continue
            el.decompose()


def extract_teachers(html: str, page_url: str, nav_words=NAV_WORDS) -> list:
    """标准提取: 教师级链接(2-4字中文名+非导航链接) + 纯文本名职对回退。
    返回 [[name, title, detail_url, anchor], ...] — anchor 保留原始链接文本
    供评估器 LLM 终审判切分正确性(不能存清洗后的名字本身)。"""
    soup = BeautifulSoup(html, "lxml")
    # 2026-08-31: 入口先剥离导航/页脚容器 — 链接循环不过滤容器,
    # 导航锚点链接(清华哲学系"近期焦点"新闻锚)会被当教师提取
    # 2026-09-18: 改 _strip_nav_guarded — 教师信号守卫防误剥(东林材料实锤)
    _strip_nav_guarded(soup)
    teachers, seen = [], set()
    # 2026-09-15 用户指令: CSS选择器优先 — 先定位教师列表容器, 容器内链接才
    # 提取(导航/侧栏/页脚在容器外, 天然免疫); 容器定位失败回退全页扫描
    # (非标准页面兼容路径, 行为与旧版一致)
    _roots = _locate_teacher_containers(soup)
    _link_iter = ((a for _r in _roots for a in _r.find_all("a", href=True))
                  if _roots else soup.find_all("a", href=True))
    for a in _link_iter:
        t = a.get_text(strip=True)
        # 2026-09-16 北林人文实锤: 教师卡片锚文本 "- 李媛辉 -" 装饰短横包裹
        # (&nbsp;-&nbsp; 形态), clean_name 剥不掉短横 → 姓名全漏; 先剥两端
        # 装饰符(短横/圆点/竖线/空白)再走后续形态识别
        # 2026-09-28 川大机械实锤: 锚文本"▪&nbsp;&nbsp;段忆翔"(实心方点装饰
        # 前缀) — ▪(U+25AA)不在原集合, 200+教师全漏。装饰符集合扩充
        # 几何符号家族(▪•●▫○◆■□)
        t = re.sub(r"^[-—–·|▪•●▫○◆■□\s]+|[-—–·|▪•●▫○◆■□\s]+$", "", t)
        # 2026-09-16 湖师大体育实锤: "姓&nbsp;&nbsp;名：周振华" 标签前缀锚文本
        # — 剥「姓名：/姓 名：」标签取纯姓名
        t = re.sub(r"^姓\s*名\s*[：:]", "", t)
        # 2026-09-22 北中医中药实锤: "中药学院导师简介-张媛" — 简介前缀+姓名
        # 形态(整站名单锚文本统一前缀), 剥离前缀取尾段姓名
        _m2 = re.match(r"^.{2,16}?(?:导师简介|简介|简历|介绍)[-–—·:\s]*([一-鿿·]{2,4})$", t)
        if _m2:
            t = _m2.group(1)
        # 2026-09-16 华农兽医实锤: 教师条目 <a title="宁章勇"><img></a>
        # 姓名在 title 属性, 锚文本空(img only) — 取 title 作姓名
        if not t:
            _tt = a.get("title", "").strip()
            if _tt and clean_name(_tt):
                t = _tt
        # 2026-08-31 北理工形态: "安丰江职位：副教授/硕导E-mail..." — 姓名与
        # 职位标签粘连, 分割取姓名段(纯姓名形态不受影响)
        if "职位：" in t or "职称：" in t:
            _t2 = re.split(r"职位：|职称：", t, 1)[0].strip()
            if re.fullmatch(r"[一-鿿·]{2,4}", _t2):
                t = _t2
        # 2026-09-15 上科大形态: "张蕴贤  教学副教授" — 姓名后空白+职称粘连,
        # 无标签分隔; 拆分姓名段并把职称填入 title 列(此前整串进 clean_name
        # 被拒, 页面 23 人只提到 7)
        # 2026-09-15 北化工机电形态: "李方俊 正高级工程师"/"高金吉 院士" —
        # 职称档组合词(正高级工程师/院士)不在原词表, 整串进 clean_name 剥
        # "高级工程师" 剩 "李方俊正" 4字假名; 长词优先(正高级工程师 > 工程师)
        _anch_title = ""
        _m = re.match(rf"^([一-鿿·]{{2,4}})\s+({_TITLE_GLUE_PAT})(\s|$)", t)
        # 2026-09-16 北航中法实锤: "李锰讲师" 姓名+职称无空白粘连 — 与有空白
        # 形态同源处理(姓名段+职称入 title 列), 否则 title 全空。
        # 姓名段非贪婪: "陈威副教授" 贪婪会切 "陈威副"+"教授"(假名), 须切
        # "陈威"+"副教授"(最长职称优先)
        if not _m:
            _m = re.match(rf"^([一-鿿·]{{1,4}}?)({_TITLE_GLUE_PAT})$", t)
        if _m:
            t = _m.group(1)
            _anch_title = _m.group(2)
        else:
            # 2026-09-16 河大国教实锤: "崔晓飞简介" 通元逐师文章列表(每教师
            # 一篇简介文章) — 姓名+简介/简历后缀, 剥后缀取姓名(title 留空)
            _mm = re.match(r"^([一-鿿·]{2,4})(?:简介|简历|个人简介|个人简历)$", t)
            if _mm:
                t = _mm.group(1)
            # 2026-09-16 辽大文学院实锤: "古代文学教研室教师：胡胜" 通元逐师
            # 文章列表变体 — 教研室/系名+教师：姓名 标签前缀, 剥前缀取姓名
            # (锚文本无教授词, 整串进 clean_name 必失败)
            _mm2 = re.match(r"^[一-鿿·]{2,12}(?:教研室|教学部|系|部|中心)?教师[：:]\s*([一-鿿·]{2,4})$", t)
            if _mm2:
                t = _mm2.group(1)
        hr = a.get("href", "").strip()
        if not hr or hr.startswith(("javascript", "#", "mailto")):
            continue
        # 站点模板坏链接: href 内嵌 HTML(转换链接错误占位) → 非法 URL, 跳过
        if "<" in hr or ">" in hr or " " in hr:
            continue
        # 导航尾词/栏目词粘连: 只对短文本判定 — 教师行混合长文本
        # ("姓名：白恩慧研究所：…" 结尾"所"会误命中导航尾词, 2026-08-31
        # 集成电路教训; 导航链接文本天然短, 长文本走 clean_name/嵌套扫描)
        # 2026-09-16 北师大哲学实锤: 教师卡片 <a> 文本"刘笑敢中国哲学研究所"
        # 以"所"结尾被 NAV_SUFFIX 误杀(9 研究所全 0 提取) — 卡片结构
        # (img + div.name/institute)不是导航, NAV_SUFFIX 命中时放行交嵌套扫描
        _is_card = False
        if len(t) <= 12:
            # 2026-09-28 川大艺术实锤: VSB 名单卡片 <a><p.name>黄宗贤</p>
            # <p.unit>美术学系</p></a> — 锚整串"黄宗贤美术学系"以系结尾被
            # NAV_SUFFIX 误杀(嵌套扫描本可救, 但 _is_card 只认 img+div.name
            # 北师大形态)。卡片判定扩到: 含 img 且含 name/institute 类子元素,
            # 或无 img 但含 p/span/div.name|unit 子元素(姓名+单位分离卡)
            _is_card = bool(
                (a.find("img") and a.find(class_=re.compile(r"(?:^|\s)(?:name|institute)(?:\s|$)")))
                or a.find(["p", "span", "div"], class_=re.compile(r"(?:^|\s)(?:name|unit)(?:\s|$)"))
            )
            if NAV_SUFFIX_RE.search(t) and not _is_card:
                continue
            if GLUE_RE.match(t):
                continue
        full = urljoin(page_url, hr)
        # 2026-10-13 机构页栏目行过滤(通用层, 1522批89FAIL实锤根修): 栏目/
        # 列表URL特征的行不落教师(DETAIL优先)。此前过滤只加在 spider_template
        # 本地函数, LLM改进生成的spider复用本通用函数 → 过滤丢失, 机构页
        # 整页栏目当教师(东大机械280/280)。本处是模板与LLM版唯一共同落点
        if not _DETAIL_URL_RE.search(full) and _ORG_URL_RE.search(full):
            continue
        # 2026-09-15 上科大实锤: 外文名形态 "Assistant Professor Brett Goodin"
        # — clean_name 只收中文, 英文名独立分支(中文粘连拆分已在上方处理)
        # 2026-09-28 川大艺术实锤: 卡片锚整串"黄宗贤美术学系"过 clean_name
        # 会截成前4字"黄宗贤美"假名 — 卡片形态不走 clean_name, 姓名由
        # 下方嵌套扫描从 p.name 子元素取
        if _is_card:
            name = ""
        elif t and not re.match(r"^[一-鿿·]", t) and re.search(r"[A-Za-z]{3,}", t):
            _eng = try_english_teacher(t)
            if _eng:
                name = _eng[1]
                if _eng[0]:
                    _anch_title = _eng[0]
            else:
                name = clean_name(t)
        else:
            name = clean_name(t)
        anchor = t[:40]
        # 图片名片式列表: 链接内无文本但 <img alt="姓名"> (浙大转化医学 teachers.html)
        # 2026-08-27: alt 走 clean_name 全量校验(GLUE/NAV_SUFFIX/SUBJECT/TITLE 兜底) —
        # 原只查 NAME_RE+nav_words, "本硕招生" 等栏目词 img alt 漏网被当教师名
        if not name or name in nav_words:
            img = a.find("img", alt=True)
            if img:
                alt = img.get("alt", "").strip()
                if alt and alt not in nav_words:
                    _n = clean_name(alt)
                    if _n:
                        name, anchor = _n, alt
        if not name or name in nav_words:
            # 2026-09-28 川大艺术 VSB 卡片: 优先取 class=name 子元素
            # (避免 unit 类子元素如"美术学系"先被通用扫描误当姓名)
            if _is_card:
                _nc = a.find(["p", "span", "div"], class_=re.compile(r"(?:^|\s)name(?:\s|$)"))
                if _nc:
                    _nct = _nc.get_text(strip=True)
                    if 2 <= len(_nct) <= 4 and re.fullmatch(r"[一-鿿·]{2,4}", _nct) \
                            and _nct not in nav_words:
                        name, anchor = _nct, _nct
        if not name or name in nav_words:
            # 2026-08-31 嵌套子元素姓名形态(清华集成电路 jyry.htm):
            # <a href="../info/1015/2267.htm"><div>姓名：白恩慧</div><span>姓名：</span>
            # <p>白恩慧</p><div>研究所：...</div></a> — 链接文本是混合长串,
            # 姓名在子元素内(常带"姓名："标签前缀)。取首个纯姓名子元素。
            for child in a.find_all(["div", "span", "p", "li", "h3", "h4"]):
                ct = child.get_text(strip=True)
                ct = re.sub(r"^(姓名|教师|导师)\s*[：:]\s*", "", ct)
                # 2026-09-16 东北悉尼实锤: h3.title 文本"傅昌锃 |A.Prof FU Changzeng"
                # 姓名|英文 形态 — 取 | 前中文姓名段
                if "|" in ct:
                    _cn = ct.split("|")[0].strip()
                    if re.fullmatch(r"[一-鿿·]{2,4}", _cn):
                        ct = _cn
                if (2 <= len(ct) <= 4 and re.fullmatch(r"[一-鿿·]{2,4}", ct)
                        and ct not in nav_words):
                    # 2026-09-07 上科大创艺: 长介绍按 span 分段排版卡片 —
                    # "…为硕</span><span>士研究</span><span>生…" 中间片段被当姓名
                    # (零容忍 FAIL)。span 是行内元素: 邻接兄弟文本以中文结尾/开头
                    # (中间无空白) = 长词切分片段, 非独立姓名; 块级(div/p/li)
                    # 兄弟如"研究所："是完整单元格, 不受此限(清华集成电路形态)。
                    if child.name == "span":
                        _ps = (str(child.previous_sibling) if not getattr(child.previous_sibling, "get_text", None)
                               else child.previous_sibling.get_text(strip=True)).strip()
                        _ns = (str(child.next_sibling) if not getattr(child.next_sibling, "get_text", None)
                               else child.next_sibling.get_text(strip=True)).strip()
                        # 2026-09-23 央财政府管理实锤: "刘庆乐"(粗体span)+
                        # "教授 硕导"(职称span)分离式姓名+职称 — 职称span以中文
                        # 开头被长词切分守卫误判(姓名全漏, 48链接只提3)。
                        # 相邻span是职称词(教授/副教授/...) = 姓名+职称对,
                        # 非长词切分片段, 放行
                        _title_ns = bool(_ns and _FRAG_TITLE_HINT.match(_ns))
                        if (_ps and re.search(r"[一-鿿·]$", _ps) and not _title_ns) \
                                or (_ns and re.search(r"^[一-鿿·]", _ns) and not _title_ns):
                            continue  # 长词切分片段
                    name, anchor = ct, ct
                    break
        if not name or len(name) < 2:
            continue
        if name in nav_words:  # 栏目/分类导航词 (专任教师/荣休教师/旧版等), 非教师姓名
            continue
        # 2026-09-16 合工大电气实锤: 教师个人主页 URL 形如
        # faculty.hfut.edu.cn/lb12/zh_CN/index.htm — 旧过滤 index\.htm 把
        # 50+ 教师全部误杀(只提 1 人)。index.htm 只拦浅路径栏目根
        # (域名后 ≤2 段, 如 /index.htm、/szdw/index.htm), 个人主页多层
        # 路径(/xx/zh_CN/index.htm)放行; list.htm 仍是栏目页铁信号
        _full_p = full[full.find("://") + 3:] if "://" in full else full
        if re.search(r"list\.htm|/xwzx/|/tzgg/|/xygk/", full):
            continue
        if re.search(r"index\.htm", full) and _full_p.count("/") <= 2:
            continue
        if name not in seen:
            seen.add(name)
            teachers.append([name, _anch_title, full, anchor])
    # 纯文本回退: 姓名+职称相邻行
    if len(teachers) < 3:
        # 2026-08-31: 剥离导航/页脚容器 — 纯文本回退扫全页行对, 导航词
        # (近期焦点/系所动态等) 会与相邻行误配对成教师(清华哲学系 9人含1噪声)
        # 2026-09-18: 改 _strip_nav_guarded — 教师信号守卫防误剥
        _strip_nav_guarded(soup)
        for t in soup.find_all(["script", "style"]):
            t.decompose()
        lines = [l.strip() for l in soup.get_text("\n", strip=True).split("\n") if l.strip()]
        title_re = re.compile(r"教授|研究员|副教授|副研究员|讲师|院士|博导|硕导|博士")
        # 2026-09-15 矿大机电实锤: 纯文本无链接列表 "葛世荣-教授" 连字符形态 —
        # NAME_RE 整行匹配要求纯姓名, "姓名-职称"同行全漏; 职称词强约束防误收
        # (新闻行"张三-获得基金"职称词不匹配)
        # 2026-09-15 华理体育实锤: 空格变体 "叶心明 教授"/"王  跃 教授"(多空格)/
        # "郑柏存 教授（退休）" — 分隔符 [连字符|空白] 二选一, 职称后边界
        # (括号|空白+余文|行尾) 防"教授团队"类粘连误收
        dash_re = re.compile(r"^([一-鿿·]{2,4})\s*(?:[-—–~]\s*|\s+|[，,]\s*)(教授|副教授|研究员|副研究员|"
                             r"讲师|高级工程师|工程师|实验师|助教|院士|博导|硕导|博士后|"
                             r"正高级工程师|一级编导|编导|一级演员|演员|导演|一级导演|"
                             r"一级美术师|高级实验师)(?:[（(].*?[）)]|\s.*|$)")
        # 2026-09-16 广西艺术实锤: "胡红一，一级编导" 姓名，职称 纯文本行
        # (无链接名单, 共4条) — 分隔符加中文逗号, 职称表补艺术序列
        comma_re = re.compile(r"^([一-鿿·]{2,4})[，,]\s*(.{1,12})$")
        # 2026-08-31 邮箱名单形态(清华哲学系 js.htm): 姓名行 + 邮箱/研究室/电话行
        # (无职称行) — 职称词不在相邻行, 原回退漏提(1/14)
        contact_re = re.compile(r"邮箱|研究室|电话|Email|信箱")
        for i, l in enumerate(lines):
            # 2026-09-16 北理工化学 gccrc 实锤: "国家级青年人才  陈磅宽" 称号前缀
            # 形态 — 职称在上一行("长聘教授/博士生导师"), 姓名行=称号+空白+姓名
            _talent = re.match(r"^[^\s，,。]{2,14}(?:人才|学者|青年)\s+([一-鿿·]{2,4})$", l)
            if _talent:
                _nm = _talent.group(1)
                if _nm not in nav_words and _nm not in seen and i > 0:
                    _prev = lines[i - 1]
                    _ttl = _prev if re.search(r"教授|研究员|导师|学者", _prev) else ""
                    seen.add(_nm)
                    teachers.append([_nm, _ttl, "", l[:40]])
                continue
            # 中文间排版空格压缩: "王  跃 教授" → "王跃 教授" (姓名内空格,
            # 华理体育实锤) — 仅用于形态匹配, anchor 存原行
            _l2 = re.sub(r"(?<=[一-鿿·])\s+(?=[一-鿿·])", "", l)
            _dm = dash_re.match(_l2)
            # 2026-09-16 广西艺术实锤: "胡红一，一级编导" — dash_re 已含逗号
            # 分隔, 若未命中再试宽形态(职称词校验防"张三，男"误收)
            if not _dm:
                _cm = comma_re.match(_l2)
                if _cm and re.search(
                        r"教授|研究员|工程师|讲师|助教|院士|博士|编导|导演|演员|美术师|级",
                        _cm.group(2)):
                    _dm = _cm
            if _dm:
                _nm = _dm.group(1)
                if _nm in nav_words or _nm in seen:
                    continue
                seen.add(_nm)
                teachers.append([_nm, _dm.group(2), "", l[:40]])
                continue
            # 2026-09-16 辽大外院实锤: "安海英，讲师；毕业于辽宁师范大学…"
            # 叙述型简历段落 — 段首「姓名，职称；」是稳定形态(；强信号,
            # 正文介绍以。句读, 不会误命中)。历史 A2 出生叙述形态的同源变体
            _nar = re.match(r"^([一-鿿·]{2,4})[，,、\s]+(教授|副教授|助理教授|讲师|"
                            r"助教|研究员|副研究员|高级工程师|工程师|实验师|"
                            r"主任医师|副主任医师|博士|博士后|院士|主任|院长|书记|"
                            r"所长|副校长|校长)[；;]", _l2)
            if _nar:
                _nm = _nar.group(1)
                if _nm not in nav_words and _nm not in seen:
                    seen.add(_nm)
                    teachers.append([_nm, _nar.group(2), "", l[:40]])
                continue
            # 2026-09-16 华电体育实锤: "姓名：张佰辉 / 职称：副教授" 标签值对
            # 名单(教师简介列表页, 每教师一组标签行) — 标签强信号直接收,
            # 职称从相邻"职称："行取
            _nl = re.search(r"^姓名[：:]\s*([一-鿿·]{2,4})(?:[\s，,、]|$)", _l2)
            # 2026-09-16 西藏大学工学院实锤: 叙述块内嵌 "1、基本信息姓名：张源
            # 职称：教授邮箱：..." — 姓名标签在行中段; 边界=职称|邮箱|电话|
            # 研究方向(标签词后必接)
            if not _nl:
                _nl = re.search(r"姓名[：:]\s*([一-鿿·]{2,4})"
                                r"(?=职称|邮箱|电话|研究方向|$|[\s，,、。])", _l2)
            if _nl:
                _nm = _nl.group(1)
                if _nm not in nav_words and _nm not in seen:
                    _zc = ""
                    # 职称捕获: 只收职称词集合, 后接标签(邮箱/电话/研究方向)
                    # 或行尾 — "职称：教授邮箱：x@y" 不能把邮箱吞进职称
                    # (西藏工学院实锤)
                    _zc_re = re.compile(
                        r"(?:职称|职务|职务职称)[：:]\s*((?:正高级工程师|教授级高级工程师|"
                        r"高级工程师|副教授|教授|研究员|副研究员|讲师|助教|实验师|工程师|"
                        r"主任医师|副主任医师|博士)(?=邮箱|电话|研究方向|姓名|教育背景|$))")
                    _zm = _zc_re.search(_l2)
                    if not _zm:
                        for _k in (i + 1, i + 2):
                            if _k < len(lines):
                                _zm = _zc_re.search(lines[_k])
                                if _zm:
                                    break
                    if _zm:
                        _zc = _zm.group(1)
                    seen.add(_nm)
                    teachers.append([_nm, _zc, "", l[:40]])
                continue
            # 2026-09-16 西藏大学工学院实锤: "索朗白姆，女，藏族，教授，任..."
            # 出生叙述形态(姓名，性别，民族，职称，正文) — 职称强词+性别锚点
            _bio = re.match(r"^([一-鿿·]{2,4})[，,]\s*(?:女|男)[，,]\s*[^，,]{0,6}[，,]"
                            r"\s*(教授|副教授|正高级工程师|高级工程师|讲师|研究员|"
                            r"助教|主任医师|副主任医师)", _l2)
            if _bio:
                _nm = _bio.group(1)
                if _nm not in nav_words and _nm not in seen:
                    seen.add(_nm)
                    teachers.append([_nm, _bio.group(2), "", l[:40]])
                continue
            # 2026-09-22 新疆化工 jsxx 页实锤: 叙述型简历列表(15页分页,
            # 人名无链接) — 行形态 "12 2024.08 江浩 教授、博士生导师"
            # (行首序号日期), 块内 "职称：教授" 标签。姓名行首带序号日期
            # NAME_RE 不命中 — 以「职称：X」标签为锚向上回溯 1-3 行找
            # 「姓名+空格+职称」形态, 职称标签本身是名单页强信号
            # 2026-09-22 新疆化工分页页实锤: 多个教师块粘连在同一行
            # (高歌块+丁成立块+陈丽娟块 — HTML &nbsp; 不换行), 单 search
            # 只收第一个块 → 页 57 人只采到 1-2 个。改: finditer 全部
            # 「职称：」标签, 每个标签回溯其前文本最后一个「姓名+职称」对
            if "职称：" in _l2 or "职称:" in _l2:
                _zc_re = re.compile(r"职称[：:]\s*(教授|副教授|研究员|副研究员|"
                                    r"讲师|助教|高级工程师|工程师|实验师|主任医师|"
                                    r"副主任医师|博士|院士)")
                _nm_re = re.compile(r"([一-鿿·]{2,4}?)\s*(副教授|教授|研究员|副研究员|"
                                    r"讲师|助教|高级工程师|工程师|实验师|博士|院士)")
                for _zm2 in _zc_re.finditer(_l2):
                    _zc2 = _zm2.group(1)
                    _before = _l2[:_zm2.start()]
                    _mj2 = list(_nm_re.finditer(_before))
                    if not _mj2:
                        for _k in (i - 1, i - 2, i - 3):
                            if _k < 0:
                                break
                            _mj2 = list(_nm_re.finditer(lines[_k]))
                            if _mj2:
                                break
                    if _mj2:
                        _nm = _mj2[-1].group(1)
                        if _nm not in nav_words and _nm not in seen:
                            seen.add(_nm)
                            teachers.append([_nm, _zc2, "", _l2[:40]])
                continue
            # 2026-09-22 山西环资 jiaos1 页实锤: 「教授」纯标签行后接
            # 连续姓名行(董川/张全喜/桑楠…20人, 每人一行无链接) — 职称
            # 标签+姓名块形态。风险控制: 标签行后 ≥2 个连续纯姓名行才收
            # (菜单职称 tab 行后接的是其他 tab 行, 不会误收)
            _tag2 = re.fullmatch(r"(教授|副教授|讲师|研究员|副研究员|助教|正高级|副高级|中级|初级)", l)
            if _tag2:
                _zc2 = _tag2.group(1)
                _buf = []
                # 2026-09-23 华师计算机 scholat 实锤: 职称档(正高级/副高级)标签
                # 同行空格姓名串("正高级 陈泯融 陈卫东 单志龙...") — 先收同行
                _rest = l[len(_tag2.group(0)):].strip()
                if _rest:
                    for _nm in re.findall(r"[一-鿿·]{2,4}", _rest):
                        if _nm not in nav_words:
                            _buf.append(_nm)
                # 再收后续行纯姓名(山西环资形态)
                for _k in range(i + 1, min(i + 60, len(lines))):
                    _ln = lines[_k].strip()
                    if NAME_RE.fullmatch(_ln) and _ln not in nav_words:
                        _buf.append(_ln)
                    else:
                        break
                if len(_buf) >= 2:
                    for _nm in _buf:
                        if _nm not in seen:
                            seen.add(_nm)
                            teachers.append([_nm, _zc2, "", _nm])
                continue
            if NAME_RE.match(l) and i + 1 < len(lines):
                _next = lines[i + 1]
                if title_re.search(_next):
                    # 2026-09-04 山大基础医学院实锤: "带头人→团队成员清单"形态名册页,
                    # 成员清单行(岳学田、魏健、…（博士后）)含"博士"→整行姓名清单被
                    # 写入职称列(8行污染)。判别: 顿号分段后, 非职称词的人名段≥2
                    # 即姓名清单(职称行如"教授、博士生导师"分段全是职称词, 不受影响;
                    # NAME_RE 带 ^$ 锚点, 整行 findall 无效, 须逐段匹配)
                    _name_hits = 0
                    for _p in re.split(r"[、,，]", _next):
                        _p2 = re.split(r"[（(]", _p)[0].strip()
                        if NAME_RE.match(_p2) and not title_re.search(_p2):
                            _name_hits += 1
                    if _name_hits >= 2:
                        continue
                    _title = _next
                elif contact_re.search(_next):
                    _title = ""
                else:
                    continue
                name = clean_name(l)
                if name and name not in seen and name not in nav_words:
                    seen.add(name)
                    teachers.append([name, _title, "", lines[i][:40]])
        # 图片名片回退: 全页 <img alt="2-4字中文"> (无链接的名片式列表)
        if len(teachers) < 3:
            for img in soup.find_all("img", alt=True):
                alt = img.get("alt", "").strip()
                _n = clean_name(alt)
                if _n and alt not in nav_words and alt not in seen:
                    seen.add(alt)
                    teachers.append([_n, "", "", alt])
        # 2026-09-16 海洋大学实锤: Word 导出段落名单 "李华军、陈旭光、蒋德志"
        # 每姓名独立 span, 、 计入分母致原扫描比率 <0.9 且 4 人 <6 阈值全漏。
        # 统一形态: 空白/、/， 分隔的姓名段落(华理体育 &nbsp; 形态、跨 span 拆
        # 碎单字合并、顿号名单三合一): 先合并相邻单字, 再剔除分隔符, 全段须
        # 是纯 2-4 字姓名(无 >4 字片段=无句子成分), ≥3 人且比率 ≥0.8 才收
        try:
            for _el in soup.find_all(["p", "li", "td"]):
                _seg = re.sub(r"[\xa0　]+", " ", _el.get_text(" ", strip=True)).strip()
                if len(_seg) < 12 or len(_seg) > 300:
                    continue
                _merged = []
                for _p in _seg.split(" "):
                    if re.fullmatch(r"[一-鿿·]", _p) and _merged \
                            and re.fullmatch(r"[一-鿿·]", _merged[-1]):
                        _merged[-1] += _p  # 单字+单字 = 跨span拆碎姓名
                    elif _p and _p not in ("、", "，", ",", ";", "-", "—",
                                           "–", "·", "|"):
                        _merged.append(_p)
                if any(len(p) > 4 for p in _merged):
                    continue
                _hits = [p for p in _merged if clean_name(p)]
                if len(_hits) >= 3 and len(_hits) / len(_merged) >= 0.8:
                    for _nm in _hits:
                        if _nm not in nav_words and _nm not in seen:
                            seen.add(_nm)
                            teachers.append([_nm, "", "", _seg[:40]])
        except Exception:
            pass
        # 2026-09-16 北航学院实锤: "冯如书院院长：杨超" 职务：姓名 文本行名单
        # (7 人, 无链接) — 全页 ≥3 条才收(防概况页单条领导行误入)
        if len(teachers) < 3:
            _post_re = re.compile(r"^[一-鿿·]{2,16}(?:院长|主任|书记|所长|处长|负责人|"
                                  r"名誉教授|特聘教授)[：:]\s*([一-鿿·]{2,4})$")
            _posts = [_post_re.match(l) for l in lines]
            if sum(1 for m in _posts if m) >= 3:
                for _pm in _posts:
                    if not _pm:
                        continue
                    _nm = _pm.group(1)
                    if _nm not in nav_words and _nm not in seen:
                        seen.add(_nm)
                        teachers.append([_nm, "", "", _pm.group(0)[:40]])
        # 2026-10-13 机构页聘任公示形态(用户指令: 机构类院系教师常经
        # 「拟聘任王晟哲博士为我…」公示进院, 页面无标准名录) — 必须带
        # 学位/职称后缀才收(防行政职务公示「拟聘任XX为院长」误收)
        if len(teachers) < 3:
            _appt_re = re.compile(r"拟聘任\s*([一-鿿·]{2,4})"
                                  r"(?:博士|教授|研究员|副教授|讲师|博士后|硕士)")
            _appts = [_appt_re.search(l) for l in lines]
            if any(_appts):
                for _am in _appts:
                    if not _am:
                        continue
                    _nm = _am.group(1)
                    if _nm not in nav_words and _nm not in seen:
                        seen.add(_nm)
                        teachers.append([_nm, "拟聘任", "", _am.group(0)[:40]])
        # 2026-09-18 攻坚批次8 天大医学部生科实锤: 姓名前置+标签链名单
        # "常津 职务: 职称: 教授 邮箱: jinchang@tju.edu.cn 查看详情" —
        # 姓名打头, 职务:/职称:/邮箱: 标签链在后(VSB 模板教师列表形态),
        # 天大族多院系(智算/未来技术/医学部)同款; 各段跨行分布, 须拼接后
        # 全串正则。≥2 条才收(防单条误入)
        if len(teachers) < 3:
            _tju_joined = " ".join(lines)
            _tju_re = re.compile(
                r"(?<![:：\w])([一-鿿·]{2,4})\s+职务[:：][^职邮]*?职称[:：]\s*"
                r"([^邮\s]{2,10}?)\s*邮箱[:：]")
            _tjs = list(_tju_re.finditer(_tju_joined))
            if len(_tjs) >= 2:
                for _tm in _tjs:
                    _nm = _tm.group(1)
                    if _nm not in nav_words and _nm not in seen:
                        seen.add(_nm)
                        teachers.append(
                            [_nm, _tm.group(2), "",
                             _tju_joined[_tm.start():_tm.start() + 60]])
    return teachers


def _table_header_map(cells):
    """表头行 → 列语义映射 {col_idx: 'name'|'zc'|'url'|...}。识别 姓名/职称 等表头词。"""
    m = {}
    for i, c in enumerate(cells):
        t = re.sub(r"[\s　]", "", c)
        if t in ("姓名", "Name", "name"):
            m[i] = "name"
        elif t in ("职称", "职务职称", "专业技术职务", "职务"):
            m[i] = "zc"
        elif t in ("主页链接", "个人主页", "个人主页链接", "链接", "URL", "主页", "url"):
            m[i] = "url"
        elif t in ("性别", "学位", "学历", "年龄", "序号"):
            m[i] = t
    return m


def extract_table_rows(html: str, page_url: str, nav_words=NAV_WORDS) -> list:
    """表格模式: tr 行 = 教师(姓名单元格 + 行内含职称词佐证)。
    复旦/厦大等站点的教师列表以表格呈现, 链接模式只能捞到栏目链接。
    2026-09-16 湖师大重点实验室实锤: Word 导出的纯文本表格(姓名列无任何
    链接), 旧逻辑"只认链接单元格"全漏 66 行名单 — 增加表头驱动列映射:
    含「姓名」表头时直接按列取姓名单元格, 无需链接佐证。"""
    soup = BeautifulSoup(html, "lxml")
    teachers, seen = [], set()
    # 表头映射按表格独立: 一个页面可能多张表(简介表+名单表)
    for tbl in soup.find_all("table"):
        trs = tbl.find_all("tr")
        hdr = {}
        _hdr_cells = []
        for tr in trs:
            cells = [td.get_text(" ", strip=True)
                     for td in tr.find_all(["td", "th"])]
            if len(cells) < 2:
                continue
            # ① 表头行: 记录列语义, 本行不算数据行 — 须先于职称词门槛
            # (表头行"序号|姓名|性别|职称"本身不含教授/博士词, 会被门槛误跳)
            _hm = _table_header_map(cells)
            if "name" in _hm.values():
                hdr = _hm
                _hdr_cells = cells
                continue
            row_text = " ".join(cells)
            # 职称词门槛: 无表头时才强制。表头驱动路径豁免 — 2026-09-16
            # 国科大前沿实锤: 976 行导师名单表 方向|姓名|联合培养单位|主页
            # 链接|招生专业, 行内无任何职称词, 门槛会全杀。豁免条件: 表头
            # 含"姓名"且另有人员语义列(职称/单位/学位/专业/方向/学科/主页)
            _hdr_strong = bool(hdr) and any(
                re.sub(r"[\s　]", "", c) != "姓名" and
                re.search(r"职称|单位|学位|专业|方向|机构|学科|主页|链接|院系|年级",
                          re.sub(r"[\s　]", "", c))
                for c in _hdr_cells)
            # 2026-09-16 哈工大马院实锤: 无表头教研室名册表
            # (教研室名 | 课程负责人：史焕翔 | 史焕翔*、彭 华*) — 行内无
            # 职称词, 姓名带 * 标记/内部空格拆字/顿号分隔; 行有教研室/负责
            # 人标记且剥净后姓名 ≥2 才收
            _roster = []
            if not hdr and re.search(r"教研室|负责人|主任|秘书|教师", row_text):
                for _c in cells:
                    _cell_names = []
                    for _p in re.split(r"[、，,;；]", _c):
                        _p2 = re.sub(r"[\s*　·]", "", _p)
                        _p2 = re.sub(r"^(?:课程负责人|负责人|主任|秘书|教师|讲师)[：:]", "", _p2)
                        _nm2 = clean_name(_p2)
                        if _nm2 and _nm2 not in nav_words:
                            _cell_names.append(_nm2)
                    _roster += _cell_names
                if len(_roster) >= 2:
                    for _nm2 in _roster:
                        if _nm2 not in seen:
                            seen.add(_nm2)
                            teachers.append([_nm2, "", "", row_text[:40]])
                    continue
            if not re.search(r"教授|研究员|副教授|讲师|工程师|博士|院士", row_text) \
                    and not _hdr_strong:
                continue
            # ② 链接单元格路径(原逻辑): 姓名带详情链接
            got = []
            for a in tr.find_all("a", href=True):
                c = a.get_text(strip=True)
                name = clean_name(c)
                if not name or name in nav_words or len(name) < 2:
                    continue
                u = urljoin(page_url, a.get("href", "").strip())
                if name not in seen:
                    seen.add(name)
                    got.append(name)
                    teachers.append([name, row_text[:60], u, c[:40]])
            if got:
                continue
            # ③ 无链接行: 表头驱动取姓名列(湖师大 Word 表形态); 无表头时
            # 行内唯一 clean_name 通过的单元格即姓名
            _tds = tr.find_all(["td", "th"])
            _ncells = [i for i, c in enumerate(cells) if clean_name(c)]
            if hdr:
                _ni = [i for i, c in enumerate(cells)
                       if hdr.get(i) == "name" and clean_name(c)]
            else:
                _ni = [i for i, c in enumerate(cells)
                       if re.fullmatch(r"[一-鿿·]{2,4}", c) and clean_name(c)]
            # 姓名单元格唯一才收(多候选=表头缺失的歧义行, 跳过防假名)
            if len(_ni) != 1:
                continue
            _c = cells[_ni[0]]
            name = clean_name(_c)
            if not name or name in nav_words:
                continue
            # 职称列(表头 职称/职务)优先作 title, 无则用整行文本
            _zc = ""
            if hdr:
                _zi = [i for i in hdr if hdr[i] == "zc"]
                if _zi and _zi[0] < len(cells):
                    _zc = cells[_zi[0]][:40]
            # 详情URL: 表头"主页链接/链接"列的 <a> href (国科大前沿 976 行
            # 导师名单表形态, 姓名列纯文本无链接)
            _du = ""
            if hdr:
                _ui = [i for i in hdr if hdr[i] == "url"]
                if _ui and _ui[0] < len(_tds):
                    _ua = _tds[_ui[0]].find("a", href=True)
                    if _ua:
                        _du = urljoin(page_url, _ua.get("href", "").strip())
            if name not in seen:
                seen.add(name)
                teachers.append([name, _zc or row_text[:60], _du, _c[:40]])
    return teachers


CARD_SELS = ["div.teacher-card", "div.teacher", "li.teacher-item", "div.pic-item",
             "li.doctor-item", "div.member", "li.member", "div.team-item",
             "div.teacher-box", "li.teacher", "div.card"]


def extract_cards(html: str, page_url: str, nav_words=NAV_WORDS) -> list:
    """卡片模式: 教师卡片容器内的姓名链接(卡片文本含职称词佐证)。"""
    soup = BeautifulSoup(html, "lxml")
    teachers, seen = [], set()
    for sel in CARD_SELS:
        for card in soup.select(sel):
            a = card.find("a", href=True)
            if not a:
                continue
            t = a.get_text(strip=True)
            name = clean_name(t)
            anchor = t[:40]
            if not name or name in nav_words:
                img = a.find("img", alt=True)
                if img:
                    alt = img.get("alt", "").strip()
                    if alt and alt not in nav_words:
                        _n = clean_name(alt)
                        if _n:
                            name, anchor = _n, alt
            if not name:
                continue
            card_text = card.get_text(" ", strip=True)
            if not re.search(r"教授|研究员|副教授|讲师|工程师|博士|院士", card_text):
                continue
            u = urljoin(page_url, a.get("href", "").strip())
            if name not in seen:
                seen.add(name)
                teachers.append([name, card_text[:60], u, anchor])
    return teachers


def _iter_js_arrays(html: str):
    """JS 字符串感知的数组提取器: 找 =[...] / key:[...] 赋值形态并括号配对。
    2026-09-16 中南生科实锤: 通元 CMS newsData 数组的字符串值内含 ](CDATA
    「]]>」), 原正则(排除 ] 的字符类)在字符串内第一个 ] 处截断 → 解析失败,
    96 人名单全漏。字符串感知配对: 遇 " 进字符串态(\\ 转义跳过), 字符串外
    才计括号。匹配成功 yield 原始数组文本。"""
    _kw = re.compile(r"(?:var|let|const)\s+\w+\s*=\s*\[|=\s*\[|:\s*\[")
    for m in _kw.finditer(html):
        _i = html.find("[", m.start())
        if _i < 0:
            continue
        _depth, _in_str, _esc = 0, False, False
        _end = None
        for _k in range(_i, len(html)):
            _c = html[_k]
            if _in_str:
                if _esc:
                    _esc = False
                elif _c == "\\":
                    _esc = True
                elif _c == '"':
                    _in_str = False
            else:
                if _c == '"':
                    _in_str = True
                elif _c == "[":
                    _depth += 1
                elif _c == "]":
                    _depth -= 1
                    if _depth == 0:
                        _end = _k
                        break
        if _end is None or _end - _i < 10:
            continue
        yield html[_i:_end + 1]


def _item_name_url_title(item: dict):
    """JS 数组 dict 条目 → (name, detail_url, title, unit)。
    兼容三类厂商形态: ① showTitle/title/name + url/link(中南 CSU 实锤)
    ② url 为嵌套对象 {asString:...}(通元 CMS) ③ fields 职称字段
    fields.zc / wbextends XML 内嵌职称。"""
    _nm = item.get("showTitle") or item.get("title") or item.get("name") \
        or item.get("tipsTitle")
    if not isinstance(_nm, str):
        _nm = ""
    _u = item.get("url") or item.get("link") or item.get("docpuburl")
    if isinstance(_u, dict):
        _u = _u.get("asString") or ""
    _u = str(_u or "").strip()
    _zc = ""
    _fl = item.get("fields")
    if isinstance(_fl, dict):
        _zc = str(_fl.get("zc") or _fl.get("jszw") or "").strip()
    if not _zc:
        _we = str(item.get("wbextends") or "")
        _mz = re.search(r'name="(?:zc|jszw)"[^>]*>\s*<!\[CDATA\[(.*?)\]\]>', _we)
        if _mz:
            _zc = _mz.group(1).strip()
    _unit = ""
    if isinstance(_fl, dict):
        _unit = str(_fl.get("yxsx") or _fl.get("dwmc") or "").strip()
    return _nm, _u, _zc, _unit


def extract_js_arrays(html: str, page_url: str, nav_words=NAV_WORDS) -> list:
    """JS数组模式: 页面脚本里的姓名数组(var xxx = [\"张三\", ...])。
    probe 检测到 js_arrays 信号的站点, 教师名单藏在脚本变量里。
    2026-09-16 湘潭公管实锤: aJson = { "data":[{title,link,type,...}] }
    对象包数组形态 — 原正则只认裸数组 var x=[...], 名单全漏。
    2026-09-16 中南生科实锤: 通元 CMS newsData 数组(字符串内含 ])+
    url 嵌套对象 {asString:...} — 字符串感知配对 + 嵌套 url 兼容。"""
    teachers, seen = [], set()
    for _raw in _iter_js_arrays(html):
        arr = None
        try:
            arr = json.loads(_raw)
        except Exception:
            parts = re.findall(r"'([^']{2,20})'|\"([^\"]{2,20})\"", _raw)
            arr = [a or b for a, b in parts]
        if not isinstance(arr, list):
            continue
        for item in arr:
            if isinstance(item, dict):
                # 2026-09-03 中南大学实锤: CSU 类 CMS 名单藏在脚本 JSON 里
                # var ret = [{"showTitle":"田红旗",...,"url":"../../info/1077/6647.htm"}]
                # 姓名+详情URL 成对存在 — 旧代码只认字符串数组, 姓名被文本模式
                # 提走(无URL)而 url 字段漏配 → 详情URL缺失率100% FAIL 整校受创
                _nm, _u, _zc, _unit = _item_name_url_title(item)
                name = clean_name(_nm)
                if not name or name in nav_words or name in seen:
                    continue
                if _u and not _u.startswith(("http://", "https://")):
                    _u = urljoin(page_url, _u)
                seen.add(name)
                teachers.append([name, _zc, _u, (_nm or "")[:40]])
                continue
            if not isinstance(item, str):
                continue
            name = clean_name(item)
            if name and name not in nav_words and name not in seen:
                seen.add(name)
                teachers.append([name, "", "", item[:40]])
    return teachers




def extract_name_grid(html: str, page_url: str, nav_words=NAV_WORDS) -> list:
    """姓名网格形态 (2026-09-24 长安公路实锤): JS渲染表格里教师是
    <span class="spanStyle"><span>沙爱民</span></span> — 无链接/无职称行,
    网格布局(width calc百分比)纯姓名。信号: spanStyle/类似表格样式类 +
    高密度纯姓名(≥4个相邻)。返回 [[name, '', '', anchor], ...]。"""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    out, seen = [], set()
    # 网格容器: spanStyle 类(长安公路) + 表格样式类变体
    for sel in ('span.spanStyle', '.spanStyle span', 'span[style*="width"]'):
        pass  # 占位: 下面统一处理
    # 直接扫所有 span/div: 文本为纯中文2-4字且非导航词, 统计密度
    cands = []
    for el in soup.find_all(['span', 'div', 'td', 'li']):
        if not el.get('style') and 'spanStyle' not in ' '.join(el.get('class') or []):
            # 无样式信号的元素可能是正文 — 仅收样式网格单元格
            continue
        if el.find(['span', 'div', 'a', 'img']):
            continue  # 含子元素 = 容器不是叶
        t = el.get_text(strip=True)
        if re.fullmatch(r"[一-鿿·]{2,4}", t) and t not in nav_words and t not in seen:
            cands.append((el, t))
    # 密度门槛: 网格名单页姓名密度高(≥4), 单点正文噪声不收
    if len(cands) < 4:
        return []
    for _el, t in cands:
        seen.add(t)
        out.append([t, "", "", t])
    return out



def extract_photocard(html: str, page_url: str, nav_words=NAV_WORDS) -> list:
    """tsites平台 photoCard 属性形态 (2026-09-24 西电/人大国关实锤):
    <a class="photoCardItem" name="保建云" vicetitle="教授" href="UUID.htm">
    姓名/职称在 a 标签属性(name/viceTitle), 详情是32位UUID.htm(无/info/特征)。
    注意 lxml 把 viceTitle 小写为 vicetitle。返回 [[name, title, url, anchor]]"""
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin
    soup = BeautifulSoup(html, "lxml")
    out, seen = [], set()
    for a in soup.find_all("a", class_="photoCardItem"):
        nm = (a.get("name") or "").strip()
        tt = (a.get("vicetitle") or a.get("viceTitle") or "").strip()
        hr = (a.get("href") or "").strip()
        if (not re.fullmatch(r"[一-鿿·]{2,4}", nm) or nm in nav_words
                or nm in seen or not hr or hr.startswith(("javascript", "#"))):
            continue
        seen.add(nm)
        out.append([nm, tt, urljoin(page_url, hr), nm])
    return out


def extract_addimg(html: str, page_url: str, nav_words=NAV_WORDS) -> list:
    """tsites平台 addimg JS数组形态 (2026-09-24 西南交大土木实锤):
    ImageScale.addimg("图片", "/wtf/zh_CN/index.htm", "王腾飞", "id")
    参数2=个人主页路径, 参数3=姓名。返回 [[name, '', home, name]]"""
    from urllib.parse import urljoin
    out, seen = [], set()
    for m in re.finditer(r'addimg\("[^"]*",\s*"([^"]+)",\s*"([一-鿿·]{2,4})",\s*"[^"]*"\)',
                         html or ""):
        home, nm = m.group(1), m.group(2)
        if nm in nav_words or nm in seen:
            continue
        seen.add(nm)
        full = urljoin(page_url, home) if home.startswith("/") else home
        out.append([nm, "", full, nm])
    return out


def extract_talent_li(html: str, page_url: str, nav_words=NAV_WORDS) -> list:
    """医院「学科及专家」talent-li 卡片形态 (2026-10-14 用户人工分析复旦中山
    医院实锤): <li class="talent-li" data-id="8715"><a href="javascript:;">
    <h1>葛均波<span>/ 主任医师</span></h1>…<h4>详情 →</h4>。医生卡片无真链接
    (javascript:;), 详情URL由 data-id 构造: doctmp/{data-id}.html (与列表页
    同目录, 如 content/200.html → doctmp/8715.html)。姓名在 h1 内,
    span 为「/ 职称」段。返回 [[name, title, url, anchor]]"""
    from urllib.parse import urljoin
    soup = BeautifulSoup(html, "lxml")
    out, seen = [], set()
    for li in soup.find_all("li", class_="talent-li"):
        did = (li.get("data-id") or "").strip()
        h1 = li.find("h1")
        if not h1 or not did:
            continue
        txt = h1.get_text(" ", strip=True)
        # "葛均波 / 主任医师" → 姓名 + 职称分离; 纯姓名 h1 走开头段正则
        # (不走 clean_name — util 版对 2 字名有守卫会误拒"张三")
        name, title = "", ""
        m = re.match(r"^([一-鿿·]{2,4})\s*/\s*(.*)$", txt)
        if m:
            name, title = m.group(1), m.group(2).strip()
        else:
            m2 = re.match(r"^([一-鿿·]{2,4})", txt)
            name = m2.group(1) if m2 else ""
        if (not re.fullmatch(r"[一-鿿·]{2,4}", name) or name in nav_words
                or name in seen):
            continue
        seen.add(name)
        # 详情URL: 与列表页同级的 doctmp/{data-id}.html (中山医院构造逻辑:
        # content/200.html 的医生详情在 ../doctmp/8715.html)
        url = urljoin(page_url, f"../doctmp/{did}.html")
        out.append([name, title, url, txt[:40]])
    return out


def extract_swiper_slide(html: str, page_url: str, nav_words=NAV_WORDS) -> list:
    """医院 swiper 轮播医生卡片形态 (2026-10-14 用户人工分析重大仁济实锤):
    <a href="/html/content/24/03/114.shtml" class="swiper-slide">
      <div class="img"><img></div><div class="name">徐强</div>
      <div class="zw">科主任、主任医师、医学博士…</div>
      <div class="context"><p>学术任职…</p></div></a>
    姓名在 div.name, 职称在 div.zw, 详情 href 正常(新窗口)。返回
    [[name, title, url, anchor]]"""
    from urllib.parse import urljoin
    soup = BeautifulSoup(html, "lxml")
    out, seen = [], set()
    # 多类名形态(东直门 li.swiper-slide.doc_li)需正则匹配非精确
    for a in soup.find_all(["a", "div", "li"], class_=re.compile(r"(?:^|\s)swiper-slide(?:\s|$)")):
        # 仅处理含 name 子元素的卡片容器(div/li 包装形态时找内部 a)
        container = a if a.name == "a" else a.find("a", href=True)
        if container is None:
            continue
        _div = a
        name_el = _div.find(class_=re.compile(r"(?:^|\s)name(?:\s|$)"))
        if name_el is None:
            continue
        nm = name_el.get_text(strip=True)
        if (not re.fullmatch(r"[一-鿿·]{2,4}", nm) or nm in nav_words
                or nm in seen):
            continue
        hr = (container.get("href") or "").strip()
        if not hr or hr.startswith(("javascript", "#")):
            continue
        zw_el = _div.find(class_=re.compile(r"(?:^|\s)zw(?:\s|$)"))
        tt = zw_el.get_text(strip=True)[:40] if zw_el else ""
        seen.add(nm)
        out.append([nm, tt, urljoin(page_url, hr), nm])
    return out


def extract_teachers_multi(html: str, page_url: str, nav_words=NAV_WORDS) -> list:
    """多模式提取分发: 链接 + 表格行 + 卡片 + JS数组, 按姓名去重合并。
    薄壳模板默认走本函数 — 平台期瓶颈(表格/卡片/JS结构)在此补齐。
    2026-09-03 中南大学实锤: 文本模式先提到姓名(无URL), JS数组模式提到
    同名+详情URL — 同名去重须"带URL行补齐无URL行", 否则 URL 永远补不进来。"""
    merged, seen = [], {}
    for fn in (extract_teachers, extract_table_rows, extract_cards,
                extract_js_arrays, extract_name_grid, extract_photocard,
                extract_addimg, extract_talent_li, extract_swiper_slide):
        try:
            for t in fn(html, page_url, nav_words):
                if not t or not t[0]:
                    continue
                if t[0] in seen:
                    _i = seen[t[0]]
                    if t[2] and not merged[_i][2]:
                        merged[_i] = t  # 旧行无详情URL, 新行带URL → 补齐
                    continue
                seen[t[0]] = len(merged)
                merged.append(t)
        except Exception:
            continue
    # 2026-10-13 ORG过滤统一落点(1624批280/280复发根修): 7个提取函数
    # (表格/卡片/JS数组/网格/照片卡)输出全部经此 merge — 栏目/列表URL行
    # 在此统一滤除(DETAIL优先)。此前只加在 extract_teachers 单函数,
    # 卡片/表格提取的栏目行漏网(东大机械280/280实锤)
    merged = [t for t in merged
              if not (t[2] and not _DETAIL_URL_RE.search(urljoin(page_url, t[2]))
                      and _ORG_URL_RE.search(urljoin(page_url, t[2])))]
    # 2026-10-14 医院科室名过滤(中山学科及专家列表页实锤): 胆道外科/急诊外科/
    # 病理科等科室名被当医生提取, LLM终审零容忍整院FAIL(47真+4脏)。形态:
    # X外科/X内科(绝不可能是人名) + 3-4字XX科(2字X科真名"马科"风险留终审)
    merged = [t for t in merged
              if not re.fullmatch(r"[一-鿿]{2,4}(?:外科|内科)|[一-鿿]{3,4}科", t[0])]
    # 2026-10-09 wp3 articles 平台名单(苏迪CMS按钮分页+系级栏目树): DOM 只有
    # 第1页(中南财大公管 DOM 15 vs 接口 total 56 实锤) — 直查接口聚合全栏目。
    # 注意: AES解码后的HTML无 generalQuery 字样(端点由JS运行时拼装),
    # 平台签名用 col_news_list + siteId
    if "col_news_list" in (html or "") and re.search(r"siteId", html or ""):
        try:
            _w = wp3_articles_roster(page_url, html)
            if _w.get("ok"):
                for _t in _w["teachers"]:
                    if not _t or not _t[0]:
                        continue
                    if _t[0] in seen:
                        _i = seen[_t[0]]
                        if _t[2] and not merged[_i][2]:
                            merged[_i] = _t
                        continue
                    seen[_t[0]] = len(merged)
                    merged.append(_t)
        except Exception:
            pass
    return merged


# ── 分页判断与枚举 (2026-08-28 完善: 三层) ──
# 1) pager_pages(): 跟随师资页真实分页链接 — 最可靠, 覆盖任意形态(子目录/后缀编号/查询参数)
# 2) list_pages():  URL 形态候选兜底(listN/子目录N/后缀编号N)
# 3) 运行时 empty_streak: 连续空/失败页停止
# 分页链接文本判断必须前缀容忍 — 多数站点渲染装饰文本:
#   "下一页>>"/"下页>"/"尾页»"/"<<上一页"/"< 下页" 等, 精确匹配必然漏检(2026-08-28 实测
#   aimshutcm 的 下一页>> 精确匹配漏检, 靠同页 尾页 才兜住)。
_PAGER_FORWARD = ("下页", "下一页", "尾页", "末页")  # 可跟随的下一页链接(文本)
_PAGER_BACK = ("首页", "第一页", "上一页", "上页")   # 仅控件存在信号, 不跟随(首页=主页, 上一页=已遍历)
_PAGER_WORDS = _PAGER_FORWARD + _PAGER_BACK


def _pager_kind(t: str) -> str:
    """分页链接文本分类 → 'forward'(下一页方向, 可跟随) / 'signal'(仅存在信号) / ''(无关)。
    容忍装饰: "下一页>>"/"下页>"/"尾页»"/"<<上一页"/"< 下页"; 纯符号 ">"/"»"/">>"=forward,
    "<"/"«"/"<<"=signal。2026-08-28 实测 aimshutcm 的 下一页>> 精确匹配漏检, 靠同页 尾页 才兜住。"""
    raw = (t or "").strip()
    if not raw:
        return ""
    t2 = re.sub(r"[><»«\s]", "", raw)
    if not t2:  # 纯符号文本
        if ">" in raw or "»" in raw:
            return "forward"
        if "<" in raw or "«" in raw:
            return "signal"
        return ""
    for w in _PAGER_FORWARD:
        if t2.startswith(w):
            return "forward"
    for w in _PAGER_BACK:
        if t2.startswith(w):
            return "signal"
    return ""


def _is_pager_text(t: str) -> bool:
    """分页控件存在信号(forward 或 signal 均算), 用于 probe 阶段标记有分页。"""
    return _pager_kind(t) != ""


def _construct_ellipsis_pages(num_map: dict, max_pages: int = 30) -> dict:
    """省略号分页 URL 构造 (2026-09-07 北大CS ALL/9.htm 反向编号实锤: 页码
    2→9,3→8,…,10→1, 中间 6-9 页省略号无链接)。页码锚点数≥3 且存在跳号时,
    若页码→URL尾号满足整数线性关系 (tail = a*page + b), 构造缺失页并返回
    {page: url} 全集; 非线性/尾号不整 → 返回 {} 防错猜。"""
    if len(num_map) < 3:
        return {}
    pages_n = sorted(num_map)
    if pages_n[-1] - pages_n[0] + 1 == len(num_map):
        return {}  # 无跳号
    if pages_n[-1] - pages_n[0] + 1 > max_pages:
        return {}

    def tail_of(u):
        # 末路径段的数字: ALL/9.htm → 9; list2.htm → 2 (数字不必然在 / 后)
        m = re.search(r"(\d+)(?=[^/]*(?:\.html?)?$)", u.rsplit("?", 1)[0])
        return int(m.group(1)) if m else 0

    pts = [(p, tail_of(num_map[p])) for p in pages_n]
    p0, t0 = pts[0]
    p1, t1 = pts[1]
    if p1 == p0:
        return {}
    a = (t1 - t0) / (p1 - p0)
    b = t0 - a * p0
    if abs(a - round(a)) > 1e-9:
        return {}
    a, b = int(round(a)), int(round(b))
    for p, t in pts[2:]:
        if a * p + b != t:
            return {}  # 非线性 → 放弃构造

    tpl = num_map[pages_n[0]]

    def make_url(pg):
        t = a * pg + b
        if t <= 0:
            return None
        m = re.search(r"(\d+)(?=[^/]*$)", tpl)
        if m:
            return tpl[:m.start()] + str(t) + tpl[m.end():]
        m2 = re.search(r"(?=\.html?$)", tpl)
        if m2:
            return tpl[:m2.start()] + str(t) + tpl[m2.start():]
        return tpl.rstrip("/") + "/" + str(t) + ".htm"

    out = {}
    for pg in range(pages_n[0], pages_n[-1] + 1):
        if pg in num_map:
            out[pg] = num_map[pg]
        else:
            u = make_url(pg)
            if u:
                out[pg] = u
    return out


def pager_pages(faculty_url: str, html: str, max_pages: int = 30, cat_fallback: bool = True) -> list:
    """分页闭包(真实链接跟随): 从师资页 HTML 分页控件收集下一页 URL(下页/下一页/尾页/
    末页/数字页码/纯符号箭头)。只跟随 forward 方向链接 — 首页/上一页不跟随(实测会混入
    站点主页 main.htm/index.htm, 2026-08-28 修)。返回去重保序列表(含起点);
    无分页控件返回 [faculty_url]。与 list_pages(URL 形态猜测)互补, spider 优先本函数。
    cat_fallback: 无真实分页时是否降级收分类页/字母索引 — 仅起点页应启用;
    沿链续走的最后一页(如 crjs/list2 已是末页)再降级会把「课程体系」类栏目
    误当分类页收进队列(2026-09-15 上科大 crjs 实锤: list2 降级收进 843课程体系)"""
    pages = [faculty_url]
    if not html:
        return pages
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        return pages
    letter_pages, cat_pages = [], []
    num_map = {}  # 页码锚点 {页码(int): url} — 省略号构造用
    for a in soup.find_all("a"):
        t = a.get_text(strip=True)
        hr = a.get("href", "").strip()
        # 2026-10-10 人大心理系实锤: 分页链接用 nohref 属性而非 href
        # (<a class="gp-page-next" nohref="index1.htm">下一页</a>, 人大系
        # 站点模板 JS 从 nohref 读值跳转) — href 空时回退 nohref
        if not hr:
            hr = a.get("nohref", "").strip()
        if not hr or hr.startswith(("javascript", "#")) or "void(0)" in hr:
            continue
        if re.match(r"^\d{1,2}$", t):
            full = urljoin(faculty_url, hr)
            num_map[int(t)] = full
        if _pager_kind(t) != "forward" and not re.match(r"^\d{1,2}$", t):
            # 2026-08-31 姓名首字母索引分页(西交数学 jsml/axmszmjs/A-Z.htm):
            # 单字母链接=按首字母检索的分页, 仅当与师资页同目录/子目录才跟随
            if re.match(r"^[A-Za-z]$", t):
                _full = urljoin(faculty_url, hr)
                _dir = faculty_url.rsplit("/", 1)[0]
                if _full.startswith(_dir + "/") and _full not in letter_pages:
                    letter_pages.append(_full)
                continue
            # 2026-09-07 分类页收集: 系/中心/实验室/研究所/学部 结尾的名单分类链接
            # 不是分页控件, 但非无关链接 — 收集供下方"无数字分页时分类优先于字母"
            # 降级(西交数学: 系7页 vs 字母26页)。此前误放在下方主分支, 而本
            # continue 已把它跳过 → cat_pages 永远为空(死代码), 分类优先从未生效
            # 2026-09-15 排除栏目词: 「课程体系」结尾"系"被误当系级分类页
            # (上科大 crjs/list2 降级收进 843课程体系 实锤) — 课程/教学/
            # 培养/制度类栏目不是教师分类入口; 括号说明形态先剥括号
            # ("机械电子工程系（机械学科）" 矿大机电实锤, 裸系名 tab 同修)
            _t2 = re.sub(r"[（(].*?[）)]", "", t).strip()
            # 2026-10-14 医院科室页收集(中山学科及专家页实锤): 科室链接
            # (肝胆肿瘤与肝移植外科/心内科/病理科)是医生名单分类页 —
            # 外科/内科/科结尾同收; 2字人名"马科"误收的代价=跟进详情页
            # 提取(单教师或0), 无害
            if re.search(r"(系|中心|实验室|研究所|学部|外科|内科|科室|科)$", _t2) and len(_t2) <= 14 \
                    and not re.search(r"课程|教学|培养|制度|章程|规范|体系|方案|计划", _t2):
                full = urljoin(faculty_url, hr)
                if full not in cat_pages:
                    cat_pages.append(full)
            continue
        full = urljoin(faculty_url, hr)
        if full not in pages:
            pages.append(full)
            if len(pages) >= max_pages:
                break
    # 2026-09-07 省略号分页构造: 可见页码锚点线性外推缺失页
    # (北大CS ALL/9.htm 反向编号: 中间页无链接, 不构造则只采到 6/10 页)
    if num_map:
        _cm = _construct_ellipsis_pages(num_map, max_pages)
        if len(_cm) > len(num_map):
            for _pg in sorted(_cm):
                _cu = _cm[_pg]
                if _cu not in pages:
                    pages.append(_cu)
                    if len(pages) >= max_pages:
                        break
    _paged = [u for u in pages if u != faculty_url]  # 数字/forward 分页已收集
    if not _paged:
        # 无真实分页链接时才降级用分类页/字母索引。字母页是同一名单的首字母
        # 子集, 与数字分页并存时冗余且空页多 — 北大CS ALL.htm 同时有 1-10
        # 数字分页(反向编号)和 A-L 字母索引, 字母分支曾覆盖数字分页致只采
        # 首页 12 人 (2026-09-07 实锤, 详见 _construct_ellipsis_pages 用例)
        if cat_fallback and cat_pages:
            # 有分类页 → 字母页退居二线(仅当无分类页时使用), 分类页优先
            pages = [faculty_url] + cat_pages[:max_pages - 1]
        elif cat_fallback and letter_pages:
            pages = [faculty_url] + letter_pages[:max_pages - 1]
    return pages


def list_pages(faculty_url: str, max_pages: int = 10) -> list:
    """分页 URL 候选生成(兜底, 与 pager_pages 互补), 支持三种形态:
    - list.htm / list2.htm / list3.htm ... (CUC类站点, 尾页是 list2.htm)
    - 页码子目录: /szll1/yywl.htm → /szll1/yywl/1.htm ... /N.htm (海大物理/海大马院)
    - 后缀编号: xxx.htm → xxx2.htm / xxx_2.htm / xxx-2.htm (织梦/万维类, 2026-08-28 补)
    spider 循环中不可达页会被 empty_streak 兜底, 多生成几页无害;
    但总量设上限, 防单页站 404 风暴(查询参数分页靠 pager_pages 跟随真实链接)。"""
    pages = [faculty_url]
    base_url = faculty_url.split("?", 1)[0]  # 查询参数形态交给 pager_pages
    m = re.match(r"^(.*/)list\.htm$", base_url)
    if m:
        pages += [f"{m.group(1)}list{i}.htm" for i in range(2, max_pages + 1)]
        return pages
    m2 = re.match(r"^(.*?)([^/]+)\.(?:html?|htm)$", base_url)
    if m2:
        base = m2.group(1) + m2.group(2)
        pages += [f"{base}/{i}.htm" for i in range(1, max_pages + 1)]
        # 后缀编号只补少量(2-4), 与子目录形态候选去重后统一截断
        if not m2.group(2).startswith("list"):
            for sep in ("", "_", "-"):
                pages += [f"{m2.group(1)}{m2.group(2)}{sep}{i}.htm" for i in (2, 3, 4)]
    seen, out = set(), []
    for u in pages:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out[:max_pages + 4]  # 主形态(max_pages+1) + 后缀变体最多补 3 个, 防 404 风暴


def write_outputs(school: str, dept: str, teachers: list):
    """统一写盘: CSV(8列) + JSON(evidence) + spider_meta(溯源hash) — 一致性不变量只在此实现一次。
    teachers 元素: [name, title, detail_url, anchor, page]
    2026-09-04 原子写: 三件套全部先落 .tmp 再 os.replace — spider 被 180s 超时
    SIGKILL 截断时不再出现 CSV 新/JSON 旧 的半成品不一致(协和生医工 CSV 38 vs
    JSON 3 实锤: 杀在 csv 写完后 json 写一半)。replace 前被杀=旧三件套完好一致。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 2026-10-09 终检过滤: LLM 生成的站点特定提取器(如苏北医院 extract_site
    # 「2-4字+职称」裸扫)不过 clean_name — 「康达学院 副教授」「诊疗中心
    # 主治医师」混入实锤。统一落盘口对每条姓名过 clean_name 终检(含
    # NAV_WORDS/NAV_SUFFIX), 三件套同步过滤 — 不违反 CSV=spider 产物
    # 不变量(本函数即产线, 与 extract 内过滤同义), 也不产生 CSV/JSON
    # 不一致(同一循环内同一批过滤)。
    teachers = [t for t in teachers
                if t and len(t) > 0 and t[0] and clean_name(str(t[0]).strip())]
    # 2026-10-02 北理工机电177→1 根因实锤: 本函数一直写 CWD 相对路径 —
    # spider 子进程 cwd=院系目录没问题, 但 fix_loop 渲染兜底直接调用时
    # cwd=项目根, 177 人写进了项目根 stray teachers.csv, 院系 CSV 保持旧
    # 1 人态(石河子法学院 26 人同源)。目标目录判定: cwd 含 spider.py =
    # spider 上下文(写 cwd); 否则 = fix_loop 上下文(写 output/学校/院系)
    if (Path.cwd() / "spider.py").exists():
        _dd = Path.cwd()
    else:
        _dd = Path("output") / school / dept
        _dd.mkdir(parents=True, exist_ok=True)
    # 2026-10-26 名单不缩减硬规则(用户实锤: 工作流修改/重试后 spider 漏栏目,
    # 更小结果覆盖更大名单 — 国科大资环 317 漏采 2217 研究生导师、上外新闻
    # 36→5): 统一落盘口与盘面 teachers.csv 并集 — 新提取行优先(同名字段更新),
    # 盘面有而新结果缺的行保留。任何重跑/渲染兜底/改进轮都不可能让名单缩小。
    _old_csv = _dd / "teachers.csv"
    if _old_csv.exists():
        try:
            _old = {}
            for _r in csv.reader(open(_old_csv, encoding="utf-8")):
                if len(_r) > 6 and _r[2] and _r[2] not in ("教师姓名",):
                    _old.setdefault(_r[2].strip(), _r)
            _new_names = {str(t[0]).strip() for t in teachers}
            _add = [[_r[2], _r[3], _r[5], _r[2], _r[6]]
                    for _nm, _r in _old.items() if _nm not in _new_names]
            if _add:
                teachers = teachers + _add
        except Exception:
            pass
    _tmp = [_dd / "teachers.csv.tmp", _dd / "teachers.json.tmp", _dd / "spider_meta.json.tmp"]
    try:
        with open(_tmp[0], "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["大学", "学院", "教师姓名", "职务职称", "所属院系", "个人详情URL", "师资页URL", "采集时间"])
            for row in teachers:
                name, title, url, anchor = row[0], row[1], row[2], row[3]
                page = row[4] if len(row) > 4 else row[2]
                w.writerow([school, dept, name, title, dept, url, page, ts])
        with open(_tmp[1], "w", encoding="utf-8") as f:
            json.dump({"school": school, "dept": dept, "total": len(teachers),
                       "teachers": [{"name": t[0], "title": t[1], "detail_url": t[2],
                                     "evidence": {"page": t[4] if len(t) > 4 else t[2],
                                                  "anchor": t[3], "selector": "faculty-page"}}
                                    for t in teachers]}, f, ensure_ascii=False, indent=2)
        # 2026-09-24 cwd敏感修复: fix_loop在项目根调用时 cwd 无 spider.py
        # → 相对路径 Errno 2 写盘全败(长安公路提取146人落盘失败实锤)。
        # spider.py 与 teachers.csv 同目录(正常spider运行cwd=院系目录;
        # fix_loop调用时靠调用方 cwd 为院系目录则直接可用, 否则找 cwd
        # 下 output/学校/院系 或跳过hash(用空hash, 溯源仅影响调试)
        _sp_src = ""
        try:
            _sp_src = open("spider.py", encoding="utf-8").read()
        except OSError:
            try:
                from pathlib import Path as _P
                _alt = _P("output") / school / dept / "spider.py"
                if _alt.exists():
                    _sp_src = _alt.read_text(encoding="utf-8")
            except Exception:
                pass
        with open(_tmp[2], "w", encoding="utf-8") as f:
            json.dump({"spider_sha256": hashlib.sha256(_sp_src.encode("utf-8")).hexdigest(),
                       "run_at": ts, "school": school, "dept": dept}, f,
                      ensure_ascii=False, indent=2)
        os.replace(_tmp[0], _dd / "teachers.csv")
        os.replace(_tmp[1], _dd / "teachers.json")
        os.replace(_tmp[2], _dd / "spider_meta.json")
    except Exception:
        for _t in _tmp:
            try:
                if os.path.exists(_t):
                    os.remove(_t)
            except OSError:
                pass
        raise


# ── 万鹏 CMS (sudy-wp / wp3) XHR 师资名单 (2026-08-28) ──
# 复旦 ai 学院等站点的师资名单不在静态 HTML, 而是页面 JS 通过
# POST {origin}/_wp3services/generalQuery?queryObj=teacherHome 拉取
# (实测 复旦 ai 学院 XHR 返回 289 人, 静态页仅 2 元素 — 用户指出的识别盲区)。
# 识别特征: 页面含 sudy-wp-siteId="NNN"; 核实(数量硬规则)与提取(spider)均需走 XHR。

def detect_wp3_siteid(html):
    """万鹏 CMS 识别: 页面含 sudy-wp-siteId="NNN" 标记 → 返回 siteId 字符串, 否则 None。"""
    m = re.search(r'sudy-wp-siteId="(\d+)"', html or "")
    return m.group(1) if m else None


def wp3_teacher_roster(page_url, siteid=None, rows=999, timeout=30):
    """万鹏 CMS 师资 XHR 接口: 返回 {"ok": bool, "teachers": [...], "total": int, "reason": str}。
    teachers 元素: {name, title, career, url}。接口失败返回 ok=False。
    origin 协议注意: 页面URL可能是 http(旧链接), 但 XHR 必须走 https 才返回数据
    (实测 复旦ai http 起源返回0人, https 返回289人) → 双协议重试。"""
    if not page_url or not page_url.startswith("http"):
        return {"ok": False, "reason": "no_url"}
    from urllib.parse import urlparse
    _p = urlparse(page_url)
    schemes = ["https", "http"] if _p.scheme != "https" else ["https"]
    headers_tpl = {
        "accept": "application/json, text/javascript, */*; q=0.01",
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "x-requested-with": "XMLHttpRequest",
        "user-agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"),
    }
    data = {
        "siteId": str(siteid or ""), "pageIndex": "1", "rows": str(rows),
        "conditions": "[]",
        "orders": '[{"field":"letter","type":"asc"}]',
        "returnInfos": ('[{"field":"title","name":"title"},{"field":"cnUrl","name":"cnUrl"},'
                        '{"field":"exField1","name":"exField1"},{"field":"exField7","name":"exField7"},'
                        '{"field":"exField9","name":"exField9"},{"field":"email","name":"email"},'
                        '{"field":"career","name":"career"}]'),
        "articleType": "1", "level": "1",
    }
    for scheme in schemes:
        origin = f"{scheme}://{_p.netloc}"
        api = f"{origin}/_wp3services/generalQuery?queryObj=teacherHome"
        headers = dict(headers_tpl, origin=origin, referer=page_url)
        try:
            import requests, urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            r = requests.post(api, data=data, headers=headers, timeout=timeout, verify=False)
            j = r.json()
        except Exception as e:
            continue
        if not isinstance(j, dict):
            continue
        rows_out = j.get("data") or j.get("list") or j.get("rows") or []
        if not isinstance(rows_out, list):
            continue
        teachers = []
        for x in rows_out:
            if not isinstance(x, dict):
                continue
            name = (x.get("title") or "").strip()
            if not name:
                continue
            teachers.append({
                "name": name,
                "title": (x.get("career") or x.get("exField9") or "").strip(),
                "url": (x.get("cnUrl") or "").strip(),
            })
        # 空名单且还有备用协议 → 继续尝试; 有数据或已无备用协议 → 返回
        if teachers or scheme == schemes[-1]:
            return {"ok": True, "teachers": teachers,
                    "total": j.get("total") or len(teachers)}
    return {"ok": False, "reason": "xhr_fail"}


# ── 内嵌师资接口识别(泛化): 万鹏wp3 + SCUT _teacherHome 双模式 ──
# SCUT 设计学院等站点: 页面含 sudy-wp-siteId(万鹏), 但师资名单走内嵌接口
# https://faculty.scut.edu.cn/_web/_platform/_teacherHome/api/deptTeacherList.rst
# (GET JSONP, 实测 62人)。泛化: 扫页面内联脚本的接口 URL + siteId。
_EMBED_API_RE = re.compile(
    r"""['"]([^'"]{10,300}(?:teacherHome|deptTeacherList|teacherList|queryObj|generalQuery)[^'"]{0,200})['"]|"""
    r"(https?://[^'\"\s]{8,300}(?:deptTeacherList|teacherHome)[^'\"\s]{0,200})""", re.I)
_EMBED_SITEID_RE = re.compile(r"""['"]?siteId['"]?\s*[:=]\s*['"]?(\d+)['"]?""")


def find_embedded_roster_api(html):
    """扫描页面内联脚本里的师资接口 URL + siteId。返回 [(api_url, siteid)] 或 []。"""
    html = html or ""
    out = []
    for m in _EMBED_API_RE.finditer(html):
        u = (m.group(1) or m.group(2) or "").strip().strip("'\"")
        if not u.startswith("http"):
            continue
        if any(k in u for k in ("teacherHome", "deptTeacherList", "generalQuery", "queryObj", "teacherList")):
            if not any(x[0] == u for x in out):
                out.append([u, None])
    for i, (u, _s) in enumerate(out):
        seg = html[max(0, html.find(u) - 500): html.find(u) + 500]
        m2 = _EMBED_SITEID_RE.search(seg)
        if m2:
            out[i] = [u, m2.group(1)]
    return [(u, s) for u, s in out]


def _fetch_embedded_api(api, siteid=None, rows=999, timeout=30):
    """GET 型师资接口(SCUT deptTeacherList.rst, JSONP)。返回 {"ok", "teachers", "total"}。"""
    params = {"siteId": str(siteid or ""), "pageIndex": "1", "rows": str(rows), "career": ""}
    headers = {
        "referer": api, "x-requested-with": "XMLHttpRequest",
        "user-agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"),
    }
    try:
        import requests, urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        r = requests.get(api, params=params, headers=headers, timeout=timeout, verify=False)
        txt = r.text
    except Exception:
        return {"ok": False, "reason": "api_fail", "teachers": [], "total": 0}
    m = re.match(r'^[^(]*\((.*)\)\s*;?\s*$', txt, re.S)
    body = m.group(1) if m else txt
    try:
        j = json.loads(body)
    except Exception:
        return {"ok": False, "reason": "bad_json", "teachers": [], "total": 0}
    if not isinstance(j, dict):
        return {"ok": False, "reason": "bad_shape", "teachers": [], "total": 0}
    rows_out = j.get("data") or j.get("list") or j.get("rows") or []
    teachers = []
    if isinstance(rows_out, list):
        for x in rows_out:
            if not isinstance(x, dict):
                continue
            name = (x.get("title") or "").strip()
            if not name:
                continue
            teachers.append({"name": name,
                             "title": (x.get("career") or x.get("post") or x.get("exField9") or "").strip(),
                             "url": (x.get("cnUrl") or "").strip()})
    return {"ok": bool(teachers), "teachers": teachers,
            "total": j.get("total") or len(teachers)}


def query_embedded_teacher_roster(page_url, html, rows=999, timeout=30):
    """识别页面内嵌师资接口并拉取名单(万鹏wp3 + SCUT _teacherHome 双模式)。
    返回 {"ok": bool, "teachers": [...], "total": int, "reason": str, "mode": str}。
    注意: SCUT 站点同时含 sudy-wp-siteId 和内嵌 deptTeacherList 接口,
    wp3 teacherHome POST 会返回 0 — 必须先试内嵌接口再试 wp3。"""
    # 1) 内嵌接口(SCUT deptTeacherList.rst 等, 最具体)
    for api, siteid in find_embedded_roster_api(html):
        if "deptTeacherList" in api or "teacherList" in api or "_web/_platform" in api:
            r = _fetch_embedded_api(api, siteid, rows=rows, timeout=timeout)
            if r.get("ok") and r.get("teachers"):
                return {**r, "mode": "embedded"}
    # 2) 万鹏 wp3(复旦 ai 学院模式)
    sid = detect_wp3_siteid(html)
    if sid:
        r = wp3_teacher_roster(page_url, siteid=sid, rows=rows, timeout=timeout)
        if r.get("ok") and r.get("teachers"):
            return {**r, "mode": "wp3"}
    return {"ok": False, "reason": "no_roster_api", "teachers": [], "total": 0, "mode": ""}


# ── API 名单数据提取规则 (2026-08-28) ──
# 与静态提取同口径: clean_name + NAV_WORDS 过滤 + 中文/外文人名校验 + 去重。
_API_FOREIGN_RE = re.compile(r"^[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ'\.\-\s]{2,40}$")


def wp3_articles_roster(page_url, html=None, rows=15, timeout=30):
    """万鹏wp3 articles列表接口 — 苏迪CMS名单页按钮式分页+系级子栏目树
    (2026-10-09 中南财大公管实锤, 用户质问「网站结构分析清楚了吗」):
    学院名单按系分子栏目(行政管理系/劳动社保系/城市经济系/应急人力系...),
    每系一个 columnId、各自分页 — 扁平走查只看当前栏漏系。
    本函数从页面 nav 提取当前栏的 sub-menu 兄弟栏目(系级 columnId),
    逐栏 POST /_wp3services/generalQuery?queryObj=articles 聚合去重,
    总人数=max(总栏total, 各栏total之和) 交叉验证。
    返回 {"ok", "teachers": [[name,title,url,anchor,page]...], "total",
          "columns": {columnId: total}, "reason"}。"""
    if not page_url or not page_url.startswith("http"):
        return {"ok": False, "reason": "no_url"}
    from urllib.parse import urlparse, quote
    import curl_cffi.requests as _cr
    _p = urlparse(page_url)
    origin = f"{_p.scheme}://{_p.netloc}"
    # columnId: URL 段 _<数字> (szdw_18732 → 18732); 无则页面 columnId=N
    # (法学院 yydhn/list.htm 无数字段, 组件 columnId=17168 在HTML内 — 实锤)
    # siteId: 页面内 siteId=N (每院不同: 公管52/法学102)
    _mc = re.search(r"_(\d{3,8})", _p.path)
    column = _mc.group(1) if _mc else ""
    site = ""
    if html:
        if not column:
            _mcol = re.search(r'columnId["\']?\s*[:=]\s*["\']?(\d{3,8})', html)
            column = _mcol.group(1) if _mcol else ""
        _ms = re.search(r'siteId["\']?\s*[:=]\s*["\']?(\d{1,6})', html)
        site = _ms.group(1) if _ms else ""
    if not column or not site:
        return {"ok": False, "reason": f"缺参数 column={column} site={site}"}
    # ── 栏目树枚举: 当前栏 nav 项的 sub-menu 兄弟栏目(系级) ──
    # 2026-10-09 用户质问实锤: 公管 5 个系子栏 + 总栏; 只查当前栏会漏系。
    # 用 BeautifulSoup 定位含当前 columnId 链接的 nav 项, 收其 sub-menu 内
    # 全部 /N/list.htm 栏目(排除党群/培养/科研等 nav 词汇链接)。
    columns = {column}
    if html and column:
        try:
            _soup = BeautifulSoup(html, "lxml")
            _anchor = None
            for _a in _soup.find_all("a", href=True):
                if re.search(rf"[/_]{column}/", _a["href"]):
                    _anchor = _a
                    break
            if _anchor is not None:
                _li = _anchor.find_parent("li")
                if _li is not None:
                    for _sa in _li.find_all("a", href=True):
                        _m2 = re.search(r"/(\d{3,8})/list\.htm", _sa["href"])
                        if not _m2:
                            continue
                        _txt = _sa.get_text(strip=True)
                        if re.search(r"党建|培养|教育|科研|合作|学生|校友|服务|培训|动态|通知|公告|指南|信箱|简介|概况|首页|新闻", _txt):
                            continue
                        columns.add(_m2.group(1))
        except Exception:
            pass
    _orders = '[{"field":"f4","type":"asc"},{"field":"letter","type":"asc"}]'
    _ret = ('[{"field":"title","pattern":[{"name":"lp","value":"30"}],"name":"title"},'
            '{"field":"f4","name":"f4"},'
            '{"field":"publishTime","pattern":[{"name":"d","value":"yyyy-MM-dd"}],"name":"publishTime"}]')
    _cond = '[{"field":"scope","value":1,"type":"eq"}]'
    _headers = {
        "accept": "application/json, text/javascript, */*; q=0.01",
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "x-requested-with": "XMLHttpRequest",
        "referer": page_url,
        "user-agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"),
    }
    teachers, seen = [], set()
    col_totals = {}
    _sum_total = 0
    for _cid in sorted(columns):
        _col_teachers = 0
        for pg in range(1, 30):
            _body = (f"siteId={site}&columnId={_cid}&pageIndex={pg}&rows={rows}"
                     f"&orders={quote(_orders)}&returnInfos={quote(_ret)}&conditions={quote(_cond)}")
            try:
                r = _cr.post(f"{origin}/_wp3services/generalQuery?queryObj=articles",
                             data=_body, timeout=timeout,
                             impersonate="chrome131", headers=_headers)
            except Exception:
                break
            if r.status_code != 200 or not r.text:
                break
            try:
                j = json.loads(r.text)
            except Exception:
                break
            if not isinstance(j, dict):
                break
            items = j.get("data") or []
            if not items:
                break
            if _cid == column:
                _sum_total = int(j.get("total") or 0)  # 总栏总数(可能聚合)
            col_totals[_cid] = int(j.get("total") or 0)
            for it in items:
                nm = (it.get("title") or "").strip()
                u = it.get("url") or it.get("wapUrl") or ""
                # 2026-10-09 新闻条目过滤: 栏目混新闻时标题是长句(与保护/四个
                # 一批实锤) — 教师名 2-6 字且过 clean_name+NAV_WORDS
                _nm2 = clean_name(nm) if len(nm) <= 6 else ""
                if not _nm2 or _nm2 in NAV_WORDS or _nm2 in seen:
                    continue
                seen.add(_nm2)
                teachers.append([_nm2, "", u, _nm2, page_url])
            _col_teachers += len(items)
            if len(items) < rows:
                break
    total = max(_sum_total, len(teachers), sum(col_totals.values()))
    return {"ok": bool(teachers), "teachers": teachers, "total": total,
            "columns": col_totals, "reason": "" if teachers else "接口无数据"}


def extract_teachers_from_api(records, page_url, nav_words=NAV_WORDS):
    """API 名单提取规则 v2(2026-08-28 双模式字段对齐):
    两种接口字段语义相反 — 万鹏wp3: {name:姓名, title:职称档(正高/副高/初中级/院士), url:详情};
    SCUT embedded: {title:姓名, career/post:职称, cnUrl:详情}。
    姓名: name 字段存在优先, 否则 title; 职称: career>post>exField9, 空缺回退职称档;
    过滤职称档词/非人名, 相对 URL 补全, 按(名,url)去重。"""
    _API_TITLE_BUCKETS = {"正高", "副高", "初中级", "初级", "中级", "高级",
                          "院士", "无", "暂无", "其他", "无职称"}
    out, seen = [], set()
    for x in records:
        if not isinstance(x, dict):
            continue
        raw = (x.get("name") or x.get("title") or "").strip()
        if not raw or raw in _API_TITLE_BUCKETS:
            continue
        name = clean_name(raw)
        if not name or name in nav_words:
            continue
        if not (NAME_RE.match(name) or _API_FOREIGN_RE.match(name)):
            continue
        title = (x.get("career") or x.get("post") or x.get("exField9") or "").strip()
        if not title or title in ("无", "暂无", "无职称"):
            _t = (x.get("title") or "").strip()
            if _t and _t not in ("无", "暂无", "无职称"):
                title = _t
        url = (x.get("cnUrl") or x.get("url") or "").strip()
        if not url.startswith("http"):
            url = urljoin(page_url, url)
        key = (name, url)
        if key in seen:
            continue
        seen.add(key)
        out.append([name, title, url, raw, page_url])
    return out

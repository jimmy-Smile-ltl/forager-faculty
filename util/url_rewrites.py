"""URL 修复规则与姓名映射——下载侧与提取侧的单一事实来源。

2026-10-08 武理工计算机 147 人提取全空事故根因: 下载侧(upstream_refresh
filter_roster)应用 _URL_FIX_RULES/_name_url_maps 改写 URL 后按改写 URL
落盘, 提取侧(detail_scraper Step 0)按名单原 URL 算文件名 → hash 不匹配
→ no_local_raw。本模块把规则收敛到一处, 两侧同源导入, 消除脱节。

规则维护纪律: 新增修复规则只改本文件的 URL_FIX_RULES(先实测改写结果
再落盘); 改名映射仍写 logs/_name_url_maps.json(爬取器直接落文件)。
"""
import json
import re
import threading
from pathlib import Path
from urllib.parse import urlsplit

_TEACHERS_DIR = Path(__file__).resolve().parent.parent / "teachers"
_MAPS_FILE = _TEACHERS_DIR / "logs" / "_name_url_maps.json"

# ══════════════════════════════════════════════════════════════════
# URL 修复规则(2026-09-28 起累积, 修复结果会记录 _url_fixes.tsv 供上游同步):
#   地大北京 scholar.cugb.edu.cn: CMS 加了一层 /scholar 目录——
#   旧 /scholarDetail/2499.shtml 全 404, 新 /scholar/scholarDetail/2499.shtml
#   实测 200 79KB。全站旧路径统一失效, 直接改写。
# ══════════════════════════════════════════════════════════════════
URL_FIX_RULES: list = [
    (re.compile(r"^scholar\.cugb\.edu\.cn$", re.I),
     lambda u: (lambda p: u.replace(p.path, "/scholar" + p.path, 1)
                if p.path.startswith("/scholarDetail/") else u)(urlsplit(u))),
    # 2026-09-28 国科大 URL 污染: 名单把教师主页/外链拼在 ~ 后
    # (~https://donghl.iccas.ac.cn / ~CHEN Yanxin)。~ 后是完整 URL 的
    # 直接提取使用; 是姓名/中文的无法修, 保持原样(归上游)。
    (re.compile(r"^people\.ucas\.ac\.cn$", re.I),
     lambda u: (lambda p: (lambda m: m.group(1) if m else u)
                (re.match(r"^/~(https?://\S+)$", p)))(urlsplit(u).path)),
    # 2026-09-28 清华电子工程系: 上游把 onclick 字典残片写进 URL,
    # 真实链接藏在 {'asString': 'http:/web.ee.tsinghua.edu.cn/...'}。
    # 提取内嵌 URL 并把 http:/ 归一为 http://。
    (re.compile(r"^www\.ee\.tsinghua\.edu\.cn$", re.I),
     lambda u: (lambda m: re.sub(r"^(https?):/(?!/)", r"\1://", m.group(1))
                if m else u)(
         re.search(r"['\"]asString['\"]\s*:\s*['\"](https?:/?/?[^'\"]+)", u))),
    # 2026-09-30 北大法学院改版: 旧 /sz/zzjs/{cd,eg,hl,mr,tz}/xxx.htm
    # 全 404, 新站在 zzjs 下加了一层 azyxjs 子栏目, ID 不变:
    # /sz/zzjs/cd/147325.htm → /sz/zzjs/azyxjs/cd/147325.htm 实测 200。
    # 全频道验证(cd/eg/hl/mr/tz 含中文名 .html)均 200, 统一改写。
    (re.compile(r"^www\.law\.pku\.edu\.cn$", re.I),
     lambda u: (lambda p: (
         u.replace(p.path, "/sz/zzjs/azyxjs/" + p.path[len("/sz/zzjs/"):], 1)
         if p.path.startswith("/sz/zzjs/")
            and not p.path.startswith("/sz/zzjs/azyxjs/") else u))(urlsplit(u))),
    # 2026-10-04 武理工计算机学院换域名: 旧 csai.whut.edu.cn 全 404,
    # 新站搬至 ai.whut.edu.cn(人工智能学院)且路径不变——旧路径换新域
    # 实测 200(4/4 随机样本验证), 纯域名改写。
    (re.compile(r"^csai\.whut\.edu\.cn$", re.I),
     lambda u: u.replace("csai.whut.edu.cn", "ai.whut.edu.cn", 1)),
    # 2026-11-04 重庆大学艺术学院: 上游起点 URL 带 www 前缀
    # (www.arts.cqu.edu.cn), 该子域 DNS 不存在(实测 curl 码6 解析失败),
    # 真实站点在裸域 arts.cqu.edu.cn(无 www, 瑞数 412 可解, iv8 2s 通)。
    # 纯域名改写, 路径保留(深页 404 由发现链从首页重找解决)。
    (re.compile(r"^www\.arts\.cqu\.edu\.cn$", re.I),
     lambda u: u.replace("www.arts.cqu.edu.cn", "arts.cqu.edu.cn", 1)),
]

# ══════════════════════════════════════════════════════════════════
# list.htm 详情页豁免(2026-10-24)
# 河海/南大/东南/长安等族每教师一个 /拼音/list.htm 详情页, 被下载侧
# _LIST_URL_RE 与提取侧 _is_person_name 按 "list.htm=列表页" 强拒——
# 全库 6,291 条 list.htm 形态 URL 实测仅 55 条(0.9%)是真列表页
# (前段=栏目词 szdw/gzry/...), 其余 6,236 条是每人一个的详情页
# (js.chd.edu.cn/gcjxxy/czy/list.htm 蔡宗琰 已实测返回完整主页)。
# 判别: /xxx/list.htm 的 xxx 段是栏目词 → 列表页(不豁免); 否则
# (姓名拼音缩写/数字ID/下划线形态) → 详情页(豁免)。
# 单 host 栏目段(le.ustc 的 qzjs 等)必须按 host 拦——同一段在其他站
# 是拼音; 分散于多 host 的短段(bj/cy/hy/xz…)经 host 分布统计确认
# 为拼音详情页, 不拦。
# ══════════════════════════════════════════════════════════════════
LIST_DIR_WORDS = frozenset((
    "szdw", "szll", "gzry", "yjry", "xyjs", "szjs", "ghjs", "cyjs",
    "jsjj", "teacher", "teachers", "team", "list", "category",
    "members",  # lxafs.ustc.edu.cn/Members/list.htm 成员列表(英文段)
))
LIST_PERSON_BLOCK_BY_HOST: dict[str, frozenset] = {
    "le.ustc.edu.cn": frozenset({"qzjs"}),            # 全职教授栏目(23 人同 URL)
    "sist.shanghaitech.edu.cn": frozenset({"szdwx"}), # 师资队伍(13 人同 URL)
    "ihuman.shanghaitech.edu.cn": frozenset({"xztd"}),# 学科团队(13 人同 URL)
    "si-mian.ecnu.edu.cn": frozenset({"qnyjy"}),      # 青年研究员栏目
    "ndpe.cpu.edu.cn": frozenset({"gjyjy"}),          # 高级研究员栏目
    "cs.gzu.edu.cn": frozenset({"jsxxxx"}),           # 教师信息栏目
    "www.tmu.edu.cn": frozenset({"lrdwsj"}),          # 历任党委书记栏目
    "math.shufe.edu.cn": frozenset({"xz"}),           # 系主任列表(2 人同 URL)
    "faculty.jnu.edu.cn": frozenset({"xz"}),
    "civil.seu.edu.cn": frozenset({"xz"}),
}
# psp 变体(暨大 faculty.jnu /lgxy/sl/list.psp 石磊 等 145 条, 前段全为
# 拼音详情页)与 htm 同判别; URL 编码段(andrea%20sartori)不在栏目词表
# → 豁免, 无影响。
_LIST_PERSON_RE = re.compile(r"/([^/]+)/list\.(?:htm|psp)$", re.I)


def is_list_htm_person(url: str) -> bool:
    """list.htm 形态 URL 是否为教师个人详情页（豁免列表页误杀）。

    只对 /xxx/list.htm 形态生效; 其他 URL 一律 False（调用方维持原有
    判定, 不改变非 list.htm 路径的既有行为）。
    """
    try:
        p = urlsplit(url)
    except Exception:
        return False
    m = _LIST_PERSON_RE.search(p.path)
    if not m:
        return False
    seg = m.group(1).lower()
    if seg in LIST_DIR_WORDS:
        return False
    host = (p.hostname or "").lower()
    return seg not in LIST_PERSON_BLOCK_BY_HOST.get(host, ())


def is_list_htm_numeric(url: str) -> bool:
    """list.htm 形态 URL 的前段是否纯数字（栏目 ID 特征）。

    纯数字段名单页/详情页两可（mphy.ustc 2841=111 人共享名单页,
    lsi.zju 25413=独享详情页）——需名单级共享度区分, 由下载侧
    filter_roster 判断（共享≥2 拒）。"""
    try:
        p = urlsplit(url)
    except Exception:
        return False
    m = _LIST_PERSON_RE.search(p.path)
    return bool(m and m.group(1).isdigit())


# 医院 CMS 文章详情页: /list/栏目ID/文章ID.html(常州一院 521 人
# /list/5931/20242.html、南昌一院 96、协和皮研所 116)——被下载侧
# _LIST_URL_RE 的 /list\b 误杀为列表页, 实为详情页(2026-10-24)。
_LIST_ARTICLE_RE = re.compile(r"/list/\d+/\d+\.html?$", re.I)


def is_list_article_url(url: str) -> bool:
    """list 目录下的两级数字 ID 文章详情页（豁免 /list\\b 误杀）。"""
    try:
        p = urlsplit(url)
    except Exception:
        return False
    return bool(_LIST_ARTICLE_RE.search(p.path))


_NAME_URL_MAPS: dict | None = None
_name_map_lock = threading.Lock()


def load_name_url_maps() -> dict:
    """2026-10-01 名录重建映射: 整站改版后旧 URL 全死且新旧 ID 无对应时,
    用渲染爬取构建 姓名→新URL 表(北大经院/河海水文/北大医学人文/武大eis)。
    惰性加载, 格式: {"host": {"姓名": "绝对URL"}} — logs/_name_url_maps.json
    """
    global _NAME_URL_MAPS
    with _name_map_lock:
        if _NAME_URL_MAPS is None:
            try:
                _NAME_URL_MAPS = json.loads(
                    _MAPS_FILE.read_text(encoding="utf-8"))
            except Exception:
                _NAME_URL_MAPS = {}
        return _NAME_URL_MAPS


def apply_url_fix(url: str) -> str | None:
    """按 URL_FIX_RULES 改写; 命中并改写返回新 URL, 否则 None。"""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except Exception:
        return None
    for host_re, fn in URL_FIX_RULES:
        if not host_re.match(host):
            continue
        fixed = fn(url)
        if fixed and fixed != url:
            return fixed
    return None


def apply_name_map(name: str, url: str) -> str | None:
    """按姓名映射改写(整站改版无 ID 对应的场景); 无命中返回 None。"""
    try:
        _m = load_name_url_maps().get(
            (urlsplit(url).hostname or "").lower()) or {}
        cand = _m.get(name)
        if cand and cand != url:
            return cand
    except Exception:
        pass
    return None


def apply_url_rewrite(name: str, url: str) -> str | None:
    """下载侧同源改写链: 先 URL_FIX_RULES, 再姓名映射。
    返回改写后 URL; 两者都无命中返回 None。"""
    fixed = apply_url_fix(url)
    if fixed:
        return fixed
    return apply_name_map(name, url)

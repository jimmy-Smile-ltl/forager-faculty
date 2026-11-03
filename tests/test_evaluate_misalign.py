"""评估侧字段错位交叉校验（发现37）回归测试。

覆盖 evaluate_teachers._misalign_hits 的新规则:
- 教育经历 的 "研究方向" 是叙述内子描述（获理学硕士学位, 研究方向: X）→ 豁免;
- 其他节标签（学术成果/开授课程/荣誉奖励/工作经历…）在 教育经历 = 整段 CV 倾倒 → 判;
- "研究方向" 编号列表 / 无教育语境的裸标签 = 独立倾倒 → 判;
- 邮箱用正则（"Engineering @ NTU" 的 @ 是项目名, 不算; 真邮箱才判）。
"""
import unittest
import importlib.util
from pathlib import Path

# 注意: 仓库里有两个 evaluate_teachers.py（teachers/ 教师评估 与
# "department teachers/" 院系评估）。全量 discover 时 Step 2/3 测试会把
# "department teachers" 插进 sys.path, 普通 import 会解析到院系版。
# 用 importlib 按绝对路径显式加载教师版, 杜绝名字解析歧义。
_TEACHERS = Path(__file__).resolve().parent.parent / "teachers"
_spec = importlib.util.spec_from_file_location(
    "teachers_evaluate", _TEACHERS / "evaluate_teachers.py")
_ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ev)

_misalign_hits = _ev._misalign_hits


def hits(edu: str):
    """只关心 教育经历 的判定结果。"""
    t = {"教育经历": edu}
    return [f for f, _ in _misalign_hits(t)]


class TestEduMisalignNarrativeClause(unittest.TestCase):
    """发现37: 教育经历 中 "研究方向" 叙述子描述 → 豁免。"""

    def test_master_then_research_direction(self):
        # 时间线叙述: 获硕士学位后接研究方向子句, 合法
        self.assertEqual(hits("论与数理统计专业，硕士，获理学硕士学位，研究方向：概率论与数理统计；2010年9月—"), [])

    def test_zhuyao_research_direction(self):
        self.assertEqual(hits("2018年获中国科学技术大学博士学位，主要研究方向为低维量子材料与器件的物性研究。"), [])

    def test_zhuli_research_direction_prose(self):
        self.assertEqual(hits("获硕士学位 研究方向 动物生态学、昆虫生态学、区域生物多样性及生态安全评估"), [])

    def test_program_name_with_at(self):
        # "Engineering @ NTU" 的 @ 是学位项目名, 非邮箱 → 不判
        self.assertEqual(hits("Computer Engineering @ NTU；硕士(M.Eng)：华中科技大学、新加坡国立大学"), [])

    def test_mpacc_program_research_direction(self):
        # "会计与财务精英研究方向" 是项目名, 非研究方向倾倒
        self.assertEqual(hits("硕士专业学位（MPAcc）会计与财务精英研究方向（非全日制） 会计硕士专业学位（Ma）"), [])


class TestEduMisalignTrueDumps(unittest.TestCase):
    """发现37: 真倾倒形态 → 判错位。"""

    def test_academic_results_section(self):
        # 学术成果 节整段进入 教育经历 = CV 倾倒
        self.assertIn("教育经历", hits("获工学博士学位。主要研究方向为纳米团簇。 学术成果：1.AtomicallyPreciseNobleMetalClusters"))

    def test_work_exp_section(self):
        self.assertIn("教育经历", hits("教育背景及工作经历 1994.07~1997.07 安徽大学生物系 工作经历 1998.07~2001.09 淮北煤炭学院"))

    def test_courses_and_awards_sections(self):
        self.assertIn("教育经历", hits("获硕士学位 研究方向 动物生态学 开授课程 普通生物学 荣誉奖励 安徽省教学成果一等奖"))

    def test_numbered_research_direction(self):
        self.assertIn("教育经历", hits("量子材料与物理研究所研究方向：1.电子显微学；2.金属化合物半导体材料"))

    def test_bare_label_no_edu_context(self):
        # 无教育语境（无学位/学校/日期）, 研究方向 是独立倾倒标签
        self.assertIn("教育经历", hits("区科协常委 上海市人工智能学会副理事长 研究方向 软件建模与分析与验证"))

    def test_email_in_edu(self):
        self.assertIn("教育经历", hits("联系方式 电子邮箱：tangrong@jlu.edu.cn 研究方向 Yang-Baxter方程、同伦代数"))

    def test_form_block_dump(self):
        self.assertIn("教育经历", hits("办公地点：A栋  E-mail：zhang_xm@163.com 领域：智能计算、计算机密码"))


class TestOtherRulesUnchanged(unittest.TestCase):
    """非 教育经历 规则保持原样。"""

    def test_course_field_contains_work_exp(self):
        t = {"讲授课程": "2015 年在职教师培训 工作经历 教务处"}
        self.assertIn("讲授课程", [f for f, _ in _misalign_hits(t)])

    def test_contact_field_contains_research_dir(self):
        t = {"联系方式": "办公室 研究方向 计算机视觉"}
        self.assertIn("联系方式", [f for f, _ in _misalign_hits(t)])

    def test_empty_value_no_hit(self):
        self.assertEqual(_misalign_hits({}), [])
        self.assertEqual(_misalign_hits({"教育经历": ""}), [])


if __name__ == "__main__":
    unittest.main()

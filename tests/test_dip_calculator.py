# -*- coding: utf-8 -*-
"""
DIP 规则引擎单元测试套件
严格覆盖《开发设计文档 v1.2》的核心要求：
1. 数据库规则驱动（非硬编码）
2. Decimal 未舍入交叉相乘判灯与端点判定（89.99% 绿，90%~110% 含端点黄，>110% 红，110.004% 标注“略高于 110%”）
3. 自费标 NOT_APPLICABLE
4. 多候选/缺参/按床日缺天数标 PENDING
"""
import sys
import os
from decimal import Decimal

# 加入项目根路径
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tools.dip_calculator import match_dip_rule, evaluate_traffic_light, get_loaded_rules

def run_tests():
    print("=" * 60)
    print(">>> 开始执行 DIP 规则引擎全量约束测试 <<<")
    print("=" * 60)

    # 1. 测试判灯函数：端点与边界条件
    print("\n--- [Test 1] Decimal 未舍入交叉相乘与临界判灯测试 ---")
    bm = Decimal("1000.00")
    
    # 89.99% -> 绿灯，略低于 90%
    res1 = evaluate_traffic_light(Decimal("899.90"), bm)
    assert res1["light"] == "绿灯", f"Expected 绿灯, got {res1['light']}"
    assert res1["boundary_note"] == "略低于 90%", f"Expected 略低于 90%, got {res1['boundary_note']}"
    print(f"PASS: 899.90 / 1000.00 -> {res1['light']}, 提示: {res1['boundary_note']}, 比率: {res1['ratio_percent']}%")

    # 90.00% -> 黄灯 (含端点)
    res2 = evaluate_traffic_light(Decimal("900.00"), bm)
    assert res2["light"] == "黄灯", f"Expected 黄灯, got {res2['light']}"
    print(f"PASS: 900.00 / 1000.00 (下限端点) -> {res2['light']}, 比率: {res2['ratio_percent']}%")

    # 110.00% -> 黄灯 (含端点)
    res3 = evaluate_traffic_light(Decimal("1100.00"), bm)
    assert res3["light"] == "黄灯", f"Expected 黄灯, got {res3['light']}"
    print(f"PASS: 1100.00 / 1000.00 (上限端点) -> {res3['light']}, 比率: {res3['ratio_percent']}%")

    # 110.004% -> 红灯，标注“略高于 110%”
    res4 = evaluate_traffic_light(Decimal("1100.04"), bm)
    assert res4["light"] == "红灯", f"Expected 红灯, got {res4['light']}"
    assert res4["boundary_note"] == "略高于 110%", f"Expected 略高于 110%, got {res4['boundary_note']}"
    print(f"PASS: 1100.04 / 1000.00 -> {res4['light']}, 提示: {res4['boundary_note']}, 比率: {res4['ratio_exact']}%")

    # 125.00% -> 红灯，无临界标注
    res5 = evaluate_traffic_light(Decimal("1250.00"), bm)
    assert res5["light"] == "红灯", f"Expected 红灯, got {res5['light']}"
    assert res5["boundary_note"] is None, f"Expected None, got {res5['boundary_note']}"
    print(f"PASS: 1250.00 / 1000.00 -> {res5['light']}, 提示: {res5['boundary_note']}, 比率: {res5['ratio_percent']}%")

    # 2. 测试自费拦截
    print("\n--- [Test 2] 自费患者拦截测试 (NOT_APPLICABLE) ---")
    res_self = evaluate_traffic_light(Decimal("3000.00"), bm, ins_type="自费")
    assert res_self["status"] == "NOT_APPLICABLE", f"Expected NOT_APPLICABLE, got {res_self['status']}"
    assert res_self["light"] == "无"
    print(f"PASS: 自费患者判灯返回: status={res_self['status']}, light={res_self['light']}, message={res_self['message']}")

    # 3. 动态从数据库读取规则并匹配测试
    print("\n--- [Test 3] 数据库驱动规则匹配与参数读取 ---")
    
    # Case A: 膝关节病 M17.9 高龄职工 (68岁)
    m_high = match_dip_rule("M17.9", age=68, ins_type="职工", op_keyword="保守")
    assert m_high["status"] == "MATCHED", f"Expected MATCHED, got {m_high}"
    assert m_high["score"] == 620.2, f"Expected 620.2, got {m_high['score']}"
    assert m_high["benchmark_amount"] == 2790.90, f"Expected 2790.90, got {m_high['benchmark_amount']}"
    print(f"PASS: M17.9 68岁职工保守治疗 -> #{m_high['rule_id']} {m_high['group_name']}, 分值: {m_high['score']}, 基准: ￥{m_high['benchmark_amount']}")

    # Case B: 膝关节病 M17.9 普通居民 (50岁，指定常规保守)
    m_norm = match_dip_rule("M17.9", age=50, ins_type="居民", op_keyword="常规保守")
    assert m_norm["status"] == "MATCHED", f"Expected MATCHED, got {m_norm}"
    assert m_norm["score"] == 394.8, f"Expected 394.8, got {m_norm['score']}"
    # 居民基准 = 394.8 * 7.0 * 0.6 = 1658.16
    assert m_norm["benchmark_amount"] == 1658.16, f"Expected 1658.16, got {m_norm['benchmark_amount']}"
    print(f"PASS: M17.9 50岁居民常规保守治疗 -> #{m_norm['rule_id']} {m_norm['group_name']}, 分值: {m_norm['score']}, 基准: ￥{m_norm['benchmark_amount']}")

    # Case B2: 膝关节病 M17.9 普通居民 (50岁，未区分中医优势与常规 -> 触发 PENDING)
    m_tcm_pending = match_dip_rule("M17.9", age=50, ins_type="居民", op_keyword="保守")
    assert m_tcm_pending["status"] == "PENDING", f"Expected PENDING, got {m_tcm_pending['status']}"
    assert m_tcm_pending["pending_reason"] == "AMBIGUOUS_TCM_ADVANTAGE"
    print(f"PASS: M17.9 50岁居民未区分中医优势 -> 标记 PENDING (原因: {m_tcm_pending['pending_reason']})，绝不盲目套用第一条")

    # Case B3: 膝关节病 M17.9 普通居民 (50岁，指定中医优势病种)
    m_tcm = match_dip_rule("M17.9", age=50, ins_type="居民", op_keyword="中医优势")
    assert m_tcm["status"] == "MATCHED", f"Expected MATCHED, got {m_tcm}"
    print(f"PASS: M17.9 50岁居民中医优势病种 -> #{m_tcm['rule_id']} {m_tcm['group_name']}, 分值: {m_tcm['score']}, 基准: ￥{m_tcm['benchmark_amount']}")

    # Case C: 缺少年龄参数触发 PENDING
    m_no_age = match_dip_rule("M17.9", age=None, ins_type="职工")
    assert m_no_age["status"] == "PENDING", f"Expected PENDING, got {m_no_age['status']}"
    assert m_no_age["pending_reason"] == "MISSING_AGE", f"Expected MISSING_AGE, got {m_no_age['pending_reason']}"
    print(f"PASS: M17.9 未提供年龄 -> 标记 PENDING (原因: {m_no_age['pending_reason']})，绝不强行盲取")

    # Case D: 结肠息肉 K63.5 未指定手术触发 PENDING
    m_no_op = match_dip_rule("K63.5", age=55, ins_type="职工")
    assert m_no_op["status"] == "PENDING", f"Expected PENDING, got {m_no_op['status']}"
    assert m_no_op["pending_reason"] == "MISSING_OPERATION", f"Expected MISSING_OPERATION, got {m_no_op['pending_reason']}"
    print(f"PASS: K63.5 未指定手术操作 -> 标记 PENDING (候选数: {len(m_no_op['candidates'])})")

    # Case E: 结肠息肉 K63.5 指定切除术+胃镜
    m_op = match_dip_rule("K63.5", age=55, ins_type="职工", op_keyword="胃镜检查")
    assert m_op["status"] == "MATCHED", f"Expected MATCHED, got {m_op}"
    print(f"PASS: K63.5 指定切除术+胃镜 -> #{m_op['rule_id']} {m_op['group_name']}, 分值: {m_op['score']}, 基准: ￥{m_op['benchmark_amount']}")

    # Case F: 颈臂综合征 M53.1 按床日康复缺少在院天数触发 PENDING
    m_bed_no_day = match_dip_rule("M53.1", age=60, ins_type="职工", op_keyword="床日")
    assert m_bed_no_day["status"] == "PENDING", f"Expected PENDING, got {m_bed_no_day['status']}"
    assert m_bed_no_day["pending_reason"] == "MISSING_BED_DAYS", f"Expected MISSING_BED_DAYS, got {m_bed_no_day['pending_reason']}"
    print(f"PASS: M53.1 按床日未传天数 -> 标记 PENDING (原因: {m_bed_no_day['pending_reason']})")

    # Case G: 颈臂综合征 M53.1 按床日康复提供天数 12 天
    m_bed_with_days = match_dip_rule("M53.1", age=60, ins_type="职工", op_keyword="床日", bed_days=12)
    assert m_bed_with_days["status"] == "MATCHED", f"Expected MATCHED, got {m_bed_with_days}"
    assert m_bed_with_days["billing_unit"] == "PER_BED_DAY"
    # 床日每日基准 = 298.3410, 12天 = 3580.09
    expected_bm = (Decimal("298.3410") * Decimal("12")).quantize(Decimal("0.01"))
    assert Decimal(str(m_bed_with_days["benchmark_amount"])) == expected_bm, f"Expected {expected_bm}, got {m_bed_with_days['benchmark_amount']}"
    print(f"PASS: M53.1 按床日 12 天 -> #{m_bed_with_days['rule_id']} {m_bed_with_days['group_name']}, 12天基准: ￥{m_bed_with_days['benchmark_amount']}")

    # Case H: 自费患者匹配
    m_self = match_dip_rule("M17.9", age=68, ins_type="自费", op_keyword="保守")
    assert m_self["status"] == "NOT_APPLICABLE", f"Expected NOT_APPLICABLE, got {m_self['status']}"
    print(f"PASS: M17.9 自费患者匹配 -> status={m_self['status']}, message={m_self['message']}")

    # Case I: 未支持参保类型（如公费医疗、工伤），严禁套用居民标准，统一标 PENDING
    print("\n--- [Test 4] 参保类型合规与险种别名对齐测试 ---")
    m_gongfei = match_dip_rule("M17.9", age=68, ins_type="公费医疗", op_keyword="保守")
    assert m_gongfei["status"] == "PENDING", f"Expected PENDING, got {m_gongfei['status']}"
    assert m_gongfei["pending_reason"] == "UNSUPPORTED_INS_TYPE:公费医疗"
    assert "benchmark_amount" not in m_gongfei
    print(f"PASS: 公费医疗拦截 -> status={m_gongfei['status']}, reason={m_gongfei['pending_reason']}")

    m_gongshang = match_dip_rule("M17.9", age=68, ins_type="工伤保险", op_keyword="保守")
    assert m_gongshang["status"] == "PENDING", f"Expected PENDING, got {m_gongshang['status']}"
    assert m_gongshang["pending_reason"] == "UNSUPPORTED_INS_TYPE:工伤保险"
    print(f"PASS: 工伤保险拦截 -> status={m_gongshang['status']}, reason={m_gongshang['pending_reason']}")

    # Case J: 【审查关键验收】“城镇居民医保”与“居民医保”必须得到完全相同的基准和灯色！
    m_res_pure = match_dip_rule("M17.9", age=68, ins_type="居民医保", op_keyword="保守")
    m_res_city = match_dip_rule("M17.9", age=68, ins_type="城镇居民医保", op_keyword="保守")
    assert m_res_pure["status"] == "MATCHED" and m_res_city["status"] == "MATCHED"
    assert m_res_pure["benchmark_amount"] == m_res_city["benchmark_amount"] == 2604.84
    assert m_res_pure["ins_type"] == m_res_city["ins_type"] == "居民"
    
    # 验证费用 2450 元时，两者均判定为黄灯（2604.84 * 0.9 = 2344.36 <= 2450 <= 2865.32）
    light_pure = evaluate_traffic_light(Decimal("2450.00"), Decimal(str(m_res_pure["benchmark_amount"])), ins_type=m_res_pure["ins_type"])
    light_city = evaluate_traffic_light(Decimal("2450.00"), Decimal(str(m_res_city["benchmark_amount"])), ins_type=m_res_city["ins_type"])
    assert light_pure["light"] == light_city["light"] == "黄灯"
    print(f"PASS [审查核验通过]: 费用 2450 元下，“居民医保”与“城镇居民医保”基准同为 ￥{m_res_pure['benchmark_amount']}，灯色同为【{light_pure['light']}】")

    # Case K: 仅有“城镇”未指明职工还是居民 -> 触发 PENDING (AMBIGUOUS_INS_TYPE)
    m_ambig = match_dip_rule("M17.9", age=68, ins_type="城镇", op_keyword="保守")
    assert m_ambig["status"] == "PENDING"
    assert m_ambig["pending_reason"] == "AMBIGUOUS_INS_TYPE"
    print(f"PASS: 仅输入“城镇” -> 触发 PENDING (原因: {m_ambig['pending_reason']})，绝不擅自假设")

    # Case L: 零基准/无效基准核心层统一拦截（动态匹配目标规则并置零，绝不硬编码固定 rule_id）
    print("\n--- [Test 5] 零基准与无效基准核心层拦截测试 ---")
    normal_hit = match_dip_rule("M17.9", age=68, ins_type="职工", op_keyword="保守")
    assert normal_hit["status"] == "MATCHED" and normal_hit["rule_id"] is not None
    target_rule_id = normal_hit["rule_id"]

    mock_rules_zero = [dict(r) for r in get_loaded_rules()]
    zeroed_count = 0
    for r in mock_rules_zero:
        if r.get("rule_id") == target_rule_id:
            r["employee_base"] = Decimal("0.00")
            r["resident_base"] = Decimal("0.00")
            zeroed_count += 1
    assert zeroed_count == 1, f"必须恰好有一条规则被置零，当前置零数: {zeroed_count}"

    import tools.dip_calculator as dip_calc
    orig_get_loaded = dip_calc.get_loaded_rules
    dip_calc.get_loaded_rules = lambda **kw: mock_rules_zero
    try:
        res_zero = dip_calc.match_dip_rule("M17.9", age=68, ins_type="职工", op_keyword="保守")
        assert res_zero["matched"] is False
        assert res_zero["status"] == "PENDING"
        assert res_zero["pending_reason"] == "INVALID_BENCHMARK"
        assert "benchmark_amount" not in res_zero
        print(f"PASS: 动态规则(#{target_rule_id})零基准核心拦截 -> matched={res_zero['matched']}, status={res_zero['status']}, reason={res_zero['pending_reason']}, 无虚假 0.00 金额")
    finally:
        dip_calc.get_loaded_rules = orig_get_loaded

    # Case M: 仅有职业状态（退休/在职）而无具体险种 -> 触发 PENDING (OCCUPATION_NOT_INSURANCE_TYPE)
    m_retire = match_dip_rule("M17.9", age=68, ins_type="已退休", op_keyword="保守")
    assert m_retire["status"] == "PENDING"
    assert m_retire["pending_reason"] == "OCCUPATION_NOT_INSURANCE_TYPE"
    print(f"PASS: 仅输入职业状态“已退休” -> 触发 PENDING (原因: {m_retire['pending_reason']})，绝不擅自假定为职工医保")

    # Case N: “退休，公费医疗”与“职工，工伤保险”优先拦截
    m_gf = match_dip_rule("M17.9", age=68, ins_type="退休，公费医疗", op_keyword="保守")
    assert m_gf["status"] == "PENDING"
    assert "UNSUPPORTED_INS_TYPE:公费医疗" in m_gf["pending_reason"]
    m_gs = match_dip_rule("M17.9", age=68, ins_type="职工，工伤保险", op_keyword="保守")
    assert m_gs["status"] == "PENDING"
    assert "UNSUPPORTED_INS_TYPE:工伤保险" in m_gs["pending_reason"]
    print(f"PASS: “退休，公费医疗”与“职工，工伤保险”均优先识别特殊险种并触发 PENDING 拦截")

    # Case O: “企业职工，参保类型待查” -> 明确标明待查，绝不强行套用职工基准
    m_unknown = match_dip_rule("M17.9", age=68, ins_type="企业职工，参保类型待查", op_keyword="保守")
    assert m_unknown["status"] == "PENDING"
    assert m_unknown["pending_reason"] == "MISSING_INS_TYPE"
    print(f"PASS: “企业职工，参保类型待查” -> 拦截待查并标记 PENDING (原因: {m_unknown['pending_reason']})")

    # Case P: 结算指令优先与否定词排除
    m_dir = match_dip_rule("M17.9", age=68, ins_type="原居民医保，本次按职工医保结算", op_keyword="保守")
    assert m_dir["status"] == "MATCHED"
    assert m_dir["benchmark_amount"] == 2790.90
    m_neg = match_dip_rule("M17.9", age=68, ins_type="职工医保，非工伤", op_keyword="保守")
    assert m_neg["status"] == "MATCHED"
    assert m_neg["benchmark_amount"] == 2790.90
    print(f"PASS: “本次按职工医保结算”及“职工医保，非工伤”均正确按职工基准 ￥2790.90 测算")

    # --- [Test 6] Round 9 审查验收 (否定险种不回退确定结果、待查冒号与多字段解耦、选择疑问句与项目自费) ---
    print("\n--- [Test 6] Round 9 审查验收 (否定险种追问、待查冒号与多字段解耦、选择疑问句与项目自费) ---")
    # 6a. 本次未按职工医保结算 -> 仅有否定险种，严禁回退为职工，必须追问并省略金额
    m_6a = match_dip_rule("M17.9", age=68, ins_type="本次未按职工医保结算", op_keyword="保守")
    assert m_6a["status"] == "PENDING"
    assert m_6a["pending_reason"] == "MISSING_INS_TYPE"
    assert m_6a.get("benchmark_amount") is None
    print("PASS: “本次未按职工医保结算” -> 拦截否定险种并标记 PENDING / MISSING_INS_TYPE，零金额输出")

    # 6b. 本次不采用居民医保 -> 仅有否定险种，严禁回退为居民，必须追问并省略金额
    m_6b = match_dip_rule("M17.9", age=68, ins_type="本次不采用居民医保", op_keyword="保守")
    assert m_6b["status"] == "PENDING"
    assert m_6b["pending_reason"] == "MISSING_INS_TYPE"
    assert m_6b.get("benchmark_amount") is None
    print("PASS: “本次不采用居民医保” -> 拦截否定险种并标记 PENDING / MISSING_INS_TYPE，零金额输出")

    # 6c. 企业职工，参保类型：待查 (含中文冒号) -> 精准识别待查，标记 PENDING
    m_6c = match_dip_rule("M17.9", age=68, ins_type="企业职工，参保类型：待查", op_keyword="保守")
    assert m_6c["status"] == "PENDING"
    assert m_6c["pending_reason"] == "MISSING_INS_TYPE"
    assert m_6c.get("benchmark_amount") is None
    print("PASS: “企业职工，参保类型：待查”(含冒号) -> 精确识别参保待查，阻断测算")

    # 6d. 企业职工，病理类型待查且参保类型待查 -> 多字段待查完全解耦，不屏蔽参保待查
    m_6d = match_dip_rule("M17.9", age=68, ins_type="企业职工，病理类型待查且参保类型待查", op_keyword="保守")
    assert m_6d["status"] == "PENDING"
    assert m_6d["pending_reason"] == "MISSING_INS_TYPE"
    assert m_6d.get("benchmark_amount") is None
    print("PASS: “企业职工，病理类型待查且参保类型待查” -> 字段解耦，正确识别参保待查")

    # 6e. 按职工医保还是按居民医保结算？ -> 选择疑问句消歧，标记 AMBIGUOUS_INS_TYPE 追问
    m_6e = match_dip_rule("M17.9", age=68, ins_type="按职工医保还是按居民医保结算？", op_keyword="保守")
    assert m_6e["status"] == "PENDING"
    assert m_6e["pending_reason"] == "AMBIGUOUS_INS_TYPE"
    assert m_6e.get("benchmark_amount") is None
    print("PASS: “按职工医保还是按居民医保结算？” -> 识别选择疑问句，标记 AMBIGUOUS_INS_TYPE")

    # 6f. 职工医保，部分项目自费 -> 项目级自费不拦截整单医保测算，正常测算职工基准 ￥2790.90
    m_6f = match_dip_rule("M17.9", age=68, ins_type="职工医保，部分项目自费", op_keyword="保守")
    assert m_6f["status"] == "MATCHED"
    assert m_6f["benchmark_amount"] == 2790.90
    print("PASS: “职工医保，部分项目自费” -> 项目级自费兼容，正常测算职工基准 ￥2790.90")

    print("\n" + "=" * 60)
    print(">>> 恭喜！全部 13 类核心测试用例 100% 通过！ <<<")
    print("=" * 60)

if __name__ == "__main__":
    run_tests()

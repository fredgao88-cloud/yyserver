# -*- coding: utf-8 -*-
"""
《开发设计文档 v1.2》Phase 1 全量验收回归测试套件 (tests/test_dip_phase1.py)
严格覆盖：
1. 显式编码与明确方案精确测算 (68岁职工 M17.9 保守 -> 620.2分, 2790.90元)
2. 宽泛病名候选追问 (68岁职工膝关节病 -> PENDING / AMBIGUOUS, 绝不盲选)
3. 报销口径主动澄清 (68岁职工膝关节病能报多少 -> 区分个人报销自负与医院DIP统筹预估)
4. 参保类型缺失阻断 (68岁膝关节病 -> PENDING / MISSING_INS_TYPE, 严禁默认职工)
5. 操作时态感知 (K63.5 拟行/已取消/已完成胃镜 -> 取消操作不加分，降级保守)
6. 钉钉渠道患者隐私盾牌 (channel=dingtalk -> 仅返回院内专属工作台入口)
7. 审计日志严格合规 (记录INTENT代码，绝不记录医生问题或患者自由文本)
"""
import sys
import os
from decimal import Decimal

# 加入项目根路径
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tools.dip_calculator import match_dip_rule, evaluate_traffic_light
from agent.llm_router import parse_local_intent

def run_phase1_tests():
    print("=" * 70)
    print(">>> 开始执行 Phase 1 核心验收回归测试套件 (tests/test_dip_phase1.py) <<<")
    print("=" * 70)

    # ---------------- 用例 1: 显式编码与保守方案精确匹配 ----------------
    print("\n--- [Case 1] 显式编码与保守方案精确匹配 ---")
    query_1 = "68岁，职工医保，诊断编码 M17.9，保守治疗。请查询医院 DIP 预估支付额。"
    res_1 = parse_local_intent(query_1)
    assert res_1["intent"] == "query_dip_rule"
    assert res_1["data"]["matched"] is True
    assert res_1["data"]["score"] == 620.2
    assert res_1["data"]["benchmark_amount"] == 2790.90
    print(f"PASS [Case 1]: 明确 M17.9 + 68岁 + 职工 + 保守 -> 唯一命中 #{res_1['data']['rule_id']}, 分值: {res_1['data']['score']}, 基准: ￥{res_1['data']['benchmark_amount']}")

    # ---------------- 用例 2: 宽泛病名缺少方案追问 ----------------
    print("\n--- [Case 2] 宽泛病名缺少方案触发 PENDING 追问 ---")
    query_2 = "68岁职工膝关节病"
    res_2 = parse_local_intent(query_2)
    assert res_2["intent"] == "need_clarification"
    assert res_2["data"]["matched"] is False
    assert res_2["data"]["status"] == "PENDING"
    assert res_2["data"]["pending_reason"] == "AMBIGUOUS_BROAD_DIAGNOSIS"
    # 严格审查断言：回复文本中彻底剔除任何硬编码金额与分值！
    assert "2790.90" not in res_2["reply"]
    assert "620.2" not in res_2["reply"]
    assert len(res_2["data"].get("candidates", [])) > 0
    print(f"PASS [Case 2]: 宽泛“膝关节病”未定手术方案 -> 触发 PENDING 追问，回复文本彻底剔除金额，附带候选列表")

    # ---------------- 用例 3: “能报多少”金额口径主动澄清 ----------------
    print("\n--- [Case 3] “能报多少”金额口径主动澄清 ---")
    query_3 = "68岁职工 M17.9 保守治疗能报多少"
    res_3 = parse_local_intent(query_3)
    assert res_3["intent"] == "need_reimbursement_clarification"
    assert "患者端个人报销" in res_3["reply"]
    assert "医院端 DIP 医保统筹支付基准" in res_3["reply"]
    # 澄清前严禁提前输出预估基准金额
    assert "2790.90" not in res_3["reply"]
    # 隐私防泄漏断言：严禁在 data 中回显原始 query
    assert "query" not in res_3["data"]
    print(f"PASS [Case 3]: 医生询问“能报多少” -> 成功前置澄清个人报销与医院DIP统筹口径，暂扣支付金额，且零患者原词泄漏")

    # ---------------- 用例 4: 参保类型缺失阻断 ----------------
    print("\n--- [Case 4] 参保类型缺失阻断 (严禁默认职工) ---")
    query_4 = "68岁膝关节病"
    res_4 = parse_local_intent(query_4)
    assert res_4["intent"] == "need_clarification"
    assert res_4["data"]["matched"] is False
    assert res_4["data"]["status"] == "PENDING"
    assert res_4["data"]["pending_reason"] == "MISSING_INS_TYPE"
    print(f"PASS [Case 4]: 未提及参保类型 -> 阻断并标记 PENDING (原因: {res_4['data']['pending_reason']})，严禁擅自默认职工")

    # ---------------- 用例 5: 操作时态感知 (已取消 vs 已完成 vs 拟行) ----------------
    print("\n--- [Case 5] 操作时态感知 (已取消 vs 拟行 vs 已完成) ---")
    
    # 5a. 已取消胃镜 -> 降级为保守治疗，不加内镜分值
    query_5a = "结肠息肉 K63.5 职工医保，原定胃镜检查，现已取消胃镜"
    res_5a = parse_local_intent(query_5a)
    assert res_5a["intent"] == "query_dip_rule"
    assert res_5a["data"]["matched"] is True
    # 结肠息肉保守治疗应为 357.0 分 (规则 #65)
    assert res_5a["data"]["score"] == 357.0
    assert "已取消" in res_5a["reply"]
    print(f"PASS [Case 5a]: 操作已取消 -> 成功回退至保守治疗分组 #{res_5a['data']['rule_id']} (分值: {res_5a['data']['score']})")

    # 5b. 拟行胃镜 -> 依规回退至保守治疗分组 (357.0 分)
    query_5b = "结肠息肉 K63.5 职工医保，拟行切除术+胃镜检查"
    res_5b = parse_local_intent(query_5b)
    assert res_5b["intent"] == "query_dip_rule"
    assert res_5b["data"]["matched"] is True
    # 修正此前写反的断言：拟行操作尚未执行，依规排除操作加分，按保守组 357.0 分测算
    assert res_5b["data"]["score"] == 357.0
    assert "拟行" in res_5b["reply"]
    print(f"PASS [Case 5b]: 拟行操作 -> 依规回退至保守治疗分组 #{res_5b['data']['rule_id']} (分值: {res_5b['data']['score']})")

    # 5c. 已完成切除术+胃镜 -> 正式手术分值
    query_5c = "结肠息肉 K63.5 职工医保，已完成切除术+胃镜检查"
    res_5c = parse_local_intent(query_5c)
    assert res_5c["intent"] == "query_dip_rule"
    assert res_5c["data"]["matched"] is True
    assert res_5c["data"]["score"] == 602.5
    print(f"PASS [Case 5c]: 已完成操作 -> 命中手术分组 #{res_5c['data']['rule_id']} (分值: {res_5c['data']['score']})")

    # ---------------- 用例 6: 钉钉渠道外部移动端隐私盾牌 (含 DINGTALK_GROUP) ----------------
    print("\n--- [Case 6] 钉钉渠道外部移动端隐私盾牌 (含 DINGTALK_GROUP) ---")
    query_6 = "帮我查一下3床张大爷现在花了多少钱"
    for ch in ("dingtalk", "DINGTALK_GROUP", "dingtalk_group", "DINGTALK"):
        res_6_ding = parse_local_intent(query_6, channel=ch)
        assert res_6_ding["intent"] == "privacy_shielded"
        assert "https://hospital-internal.local/inpatient" in res_6_ding["reply"]
        assert "张大爷" not in res_6_ding["reply"]
        assert "3床" not in res_6_ding["reply"]
        assert "query" not in res_6_ding["data"]
    print(f"PASS [Case 6]: 钉钉端/钉钉群聊查床(帮我查一下3床张大爷现在花了多少钱) -> 成功触发隐私盾牌，仅返回院内入口，绝不回显患者私密信息")

    # ---------------- 用例 7: 职业状态与特殊险种优先识别 (审查 P1 关键验收) ----------------
    print("\n--- [Case 7] 职业状态与特殊险种优先识别 ---")
    # 7a. 退休+公费医疗 -> 必须识别为公费医疗，触发 PENDING，绝不误判为职工医保，绝不输出 2790.90
    q_7a = "68岁，退休，公费医疗，M17.9，保守治疗，请查询医院 DIP 预估支付额"
    res_7a = parse_local_intent(q_7a)
    assert res_7a["intent"] == "need_clarification"
    assert res_7a["data"]["status"] == "PENDING"
    assert "2790.90" not in res_7a["reply"]
    assert "UNSUPPORTED_INS_TYPE:公费医疗" in res_7a["data"]["pending_reason"]
    print("PASS [Case 7a]: 退休+公费医疗 -> 识别为公费医疗并进入 PENDING，绝不误判为职工医保，零金额回传")

    # 7b. 仅输入职业状态“已退休” -> 主动追问险种，绝不擅自假定为职工
    q_7b = "68岁，已退休，M17.9 保守治疗"
    res_7b = parse_local_intent(q_7b)
    assert res_7b["intent"] == "need_clarification"
    assert res_7b["data"]["status"] == "PENDING"
    assert res_7b["data"]["pending_reason"] == "OCCUPATION_NOT_INSURANCE_TYPE"
    assert "2790.90" not in res_7b["reply"]
    print("PASS [Case 7b]: 仅输入“已退休”职业状态 -> 拦截并主动追问参保类型")

    # 7c. 职工+工伤保险 -> 优先识别工伤保险并进入 PENDING
    q_7c = "68岁职工，工伤保险，M17.9 保守治疗"
    res_7c = parse_local_intent(q_7c)
    assert res_7c["intent"] == "need_clarification"
    assert res_7c["data"]["status"] == "PENDING"
    assert "UNSUPPORTED_INS_TYPE:工伤保险" in res_7c["data"]["pending_reason"]
    assert "2790.90" not in res_7c["reply"]
    print("PASS [Case 7c]: 职工+工伤保险 -> 优先识别工伤保险并进入 PENDING，零虚假金额输出")

    # ---------------- 用例 8: 待查拦截、本次结算指令优先与否定描述识别 (Round 7 验收) ----------------
    print("\n--- [Case 8] 待查拦截、本次结算指令优先与否定描述识别 ---")
    # 8a. 企业职工，参保类型待查 -> 明确表示险种未知，应追问并省略金额
    q_8a = "68岁，企业职工，参保类型待查，M17.9，保守治疗"
    res_8a = parse_local_intent(q_8a)
    assert res_8a["intent"] == "need_clarification"
    assert res_8a["data"]["status"] == "PENDING"
    assert res_8a["data"]["pending_reason"] == "MISSING_INS_TYPE"
    assert "2790.90" not in res_8a["reply"]
    print("PASS [Case 8a]: “企业职工，参保类型待查” -> 拦截并追问，彻底剔除确定金额")

    # 8b. 原居民医保，本次按职工医保结算 -> 采用明确指定的本次职工医保 (2790.90元)
    q_8b = "68岁，原居民医保，本次按职工医保结算，M17.9，保守治疗"
    res_8b = parse_local_intent(q_8b)
    assert res_8b["intent"] == "query_dip_rule"
    assert res_8b["data"]["matched"] is True
    assert res_8b["data"]["benchmark_amount"] == 2790.90
    assert "2790.90" in res_8b["reply"]
    print("PASS [Case 8b]: “原居民医保，本次按职工医保结算” -> 采用本次职工医保 ￥2790.90")

    # 8c. 职工医保，非工伤 -> 识别“非工伤”否定含义，按职工医保继续 (2790.90元)
    q_8c = "68岁，职工医保，非工伤，M17.9，保守治疗"
    res_8c = parse_local_intent(q_8c)
    assert res_8c["intent"] == "query_dip_rule"
    assert res_8c["data"]["matched"] is True
    assert res_8c["data"]["benchmark_amount"] == 2790.90
    assert "2790.90" in res_8c["reply"]
    print("PASS [Case 8c]: “职工医保，非工伤” -> 排除非工伤否定词，按职工医保正常测算 ￥2790.90")

    # 8d. 本次职工医保结算，既往工伤已结案 -> 识别既往工伤已结案，按职工医保继续 (2790.90元)
    q_8d = "68岁，本次职工医保结算，既往工伤已结案，M17.9，保守治疗"
    res_8d = parse_local_intent(q_8d)
    assert res_8d["intent"] == "query_dip_rule"
    assert res_8d["data"]["matched"] is True
    assert res_8d["data"]["benchmark_amount"] == 2790.90
    assert "2790.90" in res_8d["reply"]
    print("PASS [Case 8d]: “本次职工医保结算，既往工伤已结案” -> 排除既往工伤，按职工医保正常测算 ￥2790.90")

    # ---------------- 用例 9: Round 8 审查验收 (分句时态冲突、带空格待查、非医保字段保护与历史待查消除) ----------------
    print("\n--- [Case 9] Round 8 审查验收 (分句时态冲突、带空格待查、非医保字段保护与历史待查消除) ---")
    # 9a. 企业职工，参保类型 待查 (带空格) -> 必须正确拦截并省略金额
    q_9a = "68岁，企业职工，参保类型 待查，M17.9，保守治疗"
    res_9a = parse_local_intent(q_9a)
    assert res_9a["intent"] == "need_clarification"
    assert res_9a["data"]["status"] == "PENDING"
    assert res_9a["data"]["pending_reason"] == "MISSING_INS_TYPE"
    assert "2790.90" not in res_9a["reply"]
    print("PASS [Case 9a]: “企业职工，参保类型 待查”(含空格) -> 拦截并追问，彻底省略确定金额")

    # 9b. 既往按居民医保结算，本次按职工医保结算 -> 优先采用本次肯定指令 (2790.90元)，绝不错选既往居民 (2604.84元)
    q_9b = "68岁，既往按居民医保结算，本次按职工医保结算，M17.9，保守治疗"
    res_9b = parse_local_intent(q_9b)
    assert res_9b["intent"] == "query_dip_rule"
    assert res_9b["data"]["matched"] is True
    assert res_9b["data"]["benchmark_amount"] == 2790.90
    assert "2790.90" in res_9b["reply"]
    print("PASS [Case 9b]: “既往按居民医保结算，本次按职工医保结算” -> 排除既往分句，准确采用本次职工基准 ￥2790.90")

    # 9c. 本次不按职工医保结算，按居民医保结算 -> 排除否定分句，准确采用居民基准 (2604.84元)
    q_9c = "68岁，本次不按职工医保结算，按居民医保结算，M17.9，保守治疗"
    res_9c = parse_local_intent(q_9c)
    assert res_9c["intent"] == "query_dip_rule"
    assert res_9c["data"]["matched"] is True
    assert res_9c["data"]["benchmark_amount"] == 2604.84
    assert "2604.84" in res_9c["reply"]
    print("PASS [Case 9c]: “本次不按职工医保结算，按居民医保结算” -> 排除否定分句，准确采用居民基准 ￥2604.84")

    # 9d. 职工医保，病理类型待查 -> 非医保字段待查绝不误拦截，按职工医保正常测算 (2790.90元)
    q_9d = "68岁，职工医保，病理类型待查，M17.9，保守治疗"
    res_9d = parse_local_intent(q_9d)
    assert res_9d["intent"] == "query_dip_rule"
    assert res_9d["data"]["matched"] is True
    assert res_9d["data"]["benchmark_amount"] == 2790.90
    assert "2790.90" in res_9d["reply"]
    print("PASS [Case 9d]: “职工医保，病理类型待查” -> 仅限定参保字段待查，病理类型待查不误拦截 ￥2790.90")

    # 9e. 原参保类型待查，现已确认本次按职工医保结算 -> 历史待查被当前确认消除，按职工医保测算 (2790.90元)
    q_9e = "68岁，原参保类型待查，现已确认本次按职工医保结算，M17.9，保守治疗"
    res_9e = parse_local_intent(q_9e)
    assert res_9e["intent"] == "query_dip_rule"
    assert res_9e["data"]["matched"] is True
    assert res_9e["data"]["benchmark_amount"] == 2790.90
    assert "2790.90" in res_9e["reply"]
    print("PASS [Case 9e]: “原参保类型待查，现已确认本次按职工医保结算” -> 历史待查不压制当前确认 ￥2790.90")

    # ---------------- 用例 10: Round 9 审查验收 (否定险种不回退确定结果、待查冒号与多字段解耦、选择疑问句与项目自费) ----------------
    print("\n--- [Case 10] Round 9 审查验收 (否定险种追问、待查冒号与多字段解耦、选择疑问句与项目自费) ---")
    # 10a. 本次未按职工医保结算 -> 仅排除险种未说明实际险种，严禁回退为职工，必须追问并省略金额
    q_10a = "68岁，本次未按职工医保结算，M17.9，保守治疗"
    res_10a = parse_local_intent(q_10a)
    assert res_10a["intent"] == "need_clarification"
    assert res_10a["data"]["status"] == "PENDING"
    assert res_10a["data"]["pending_reason"] == "MISSING_INS_TYPE"
    assert "2790.90" not in res_10a["reply"]
    print("PASS [Case 10a]: “本次未按职工医保结算” -> 仅有否定险种，拦截并追问，彻底省略金额")

    # 10b. 本次不采用居民医保 -> 仅排除险种未说明实际险种，严禁回退为居民，必须追问并省略金额
    q_10b = "68岁，本次不采用居民医保，M17.9，保守治疗"
    res_10b = parse_local_intent(q_10b)
    assert res_10b["intent"] == "need_clarification"
    assert res_10b["data"]["status"] == "PENDING"
    assert res_10b["data"]["pending_reason"] == "MISSING_INS_TYPE"
    assert "2604.84" not in res_10b["reply"]
    print("PASS [Case 10b]: “本次不采用居民医保” -> 仅有否定险种，拦截并追问，彻底省略金额")

    # 10c. 企业职工，参保类型：待查 (含中文冒号) -> 精准识别待查，拦截并追问
    q_10c = "68岁，企业职工，参保类型：待查，M17.9，保守治疗"
    res_10c = parse_local_intent(q_10c)
    assert res_10c["intent"] == "need_clarification"
    assert res_10c["data"]["status"] == "PENDING"
    assert res_10c["data"]["pending_reason"] == "MISSING_INS_TYPE"
    assert "2790.90" not in res_10c["reply"]
    print("PASS [Case 10c]: “企业职工，参保类型：待查”(含冒号) -> 正确识别待查，阻断测算")

    # 10d. 企业职工，病理类型待查且参保类型待查 -> 多字段待查完全解耦，不屏蔽参保待查
    q_10d = "68岁，企业职工，病理类型待查且参保类型待查，M17.9，保守治疗"
    res_10d = parse_local_intent(q_10d)
    assert res_10d["intent"] == "need_clarification"
    assert res_10d["data"]["status"] == "PENDING"
    assert res_10d["data"]["pending_reason"] == "MISSING_INS_TYPE"
    assert "2790.90" not in res_10d["reply"]
    print("PASS [Case 10d]: “企业职工，病理类型待查且参保类型待查” -> 字段解耦，拦截并追问")

    # 10e. 按职工医保还是按居民医保结算？ -> 选择疑问句消歧，标记 AMBIGUOUS_INS_TYPE 追问
    q_10e = "68岁，按职工医保还是按居民医保结算？M17.9，保守治疗"
    res_10e = parse_local_intent(q_10e)
    assert res_10e["intent"] == "need_clarification"
    assert res_10e["data"]["status"] == "PENDING"
    assert res_10e["data"]["pending_reason"] == "AMBIGUOUS_INS_TYPE"
    assert "2790.90" not in res_10e["reply"]
    assert "2604.84" not in res_10e["reply"]
    print("PASS [Case 10e]: “按职工医保还是按居民医保结算？” -> 识别选择疑问句，拦截追问")

    # 10f. 职工医保，部分项目自费 -> 项目级自费不拦截整单医保测算，正常测算职工基准 ￥2790.90
    q_10f = "68岁，职工医保，部分项目自费，M17.9，保守治疗"
    res_10f = parse_local_intent(q_10f)
    assert res_10f["intent"] == "query_dip_rule"
    assert res_10f["data"]["matched"] is True
    assert res_10f["data"]["benchmark_amount"] == 2790.90
    assert "2790.90" in res_10f["reply"]
    print("PASS [Case 10f]: “职工医保，部分项目自费” -> 项目级自费兼容，正常测算职工基准 ￥2790.90")

    # ---------------- 用例 11: Round 10 审查验收 (城乡居民别名否定、单字'或'与方案选择消歧、更正为覆盖、耗材自费隔离) ----------------
    print("\n--- [Case 11] Round 10 审查验收 (城乡居民别名否定、单字'或'与方案选择消歧、更正为覆盖、耗材自费隔离) ---")
    # 11a. 本次未按城乡居民医保结算 -> 否定规则必须支持“城乡居民”复合别名，严禁错选居民基准 2604.84
    q_11a = "68岁，本次未按城乡居民医保结算，M17.9，保守治疗"
    res_11a = parse_local_intent(q_11a)
    assert res_11a["intent"] == "need_clarification"
    assert res_11a["data"]["status"] == "PENDING"
    assert res_11a["data"]["pending_reason"] == "MISSING_INS_TYPE"
    assert "2604.84" not in res_11a["reply"]
    print("PASS [Case 11a]: “本次未按城乡居民医保结算” -> 统一别名否定识别，标记 PENDING / MISSING_INS_TYPE，零金额输出")

    # 11b. 按职工医保或居民医保结算？ -> 单字“或”选择疑问句消歧，标记 AMBIGUOUS_INS_TYPE
    q_11b = "68岁，按职工医保或居民医保结算？M17.9，保守治疗"
    res_11b = parse_local_intent(q_11b)
    assert res_11b["intent"] == "need_clarification"
    assert res_11b["data"]["status"] == "PENDING"
    assert res_11b["data"]["pending_reason"] == "AMBIGUOUS_INS_TYPE"
    assert "2790.90" not in res_11b["reply"]
    assert "2604.84" not in res_11b["reply"]
    print("PASS [Case 11b]: “按职工医保或居民医保结算？” -> 单字'或'精准触发 AMBIGUOUS_INS_TYPE")

    # 11c. 本次按职工医保结算，核实后更正为居民医保 -> “更正为”强力覆盖先前的职工指令，采用居民基准 ￥2604.84
    q_11c = "68岁，本次按职工医保结算，核实后更正为居民医保，M17.9，保守治疗"
    res_11c = parse_local_intent(q_11c)
    assert res_11c["intent"] == "query_dip_rule"
    assert res_11c["data"]["matched"] is True
    assert res_11c["data"]["benchmark_amount"] == 2604.84
    assert "2604.84" in res_11c["reply"]
    print("PASS [Case 11c]: “本次按职工医保结算，核实后更正为居民医保” -> 更正指令优先，准确采用居民基准 ￥2604.84")

    # 11d. 职工医保，耗材按自费项目结算 -> 耗材范围自费不拦截整单医保，正常测算职工基准 ￥2790.90
    q_11d = "68岁，职工医保，耗材按自费项目结算，M17.9，保守治疗"
    res_11d = parse_local_intent(q_11d)
    assert res_11d["intent"] == "query_dip_rule"
    assert res_11d["data"]["matched"] is True
    assert res_11d["data"]["benchmark_amount"] == 2790.90
    assert "2790.90" in res_11d["reply"]
    print("PASS [Case 11d]: “职工医保，耗材按自费项目结算” -> 耗材支付范围隔离，正常测算职工基准 ￥2790.90")

    # 11e. 本次按职工医保结算，保守还是手术？ -> 临床方案选择绝不误触发险种追问，正常测算职工基准 ￥2790.90
    q_11e = "68岁，本次按职工医保结算，M17.9，保守还是手术？"
    res_11e = parse_local_intent(q_11e)
    assert res_11e["intent"] == "query_dip_rule"
    assert res_11e["data"]["matched"] is True
    assert res_11e["data"]["benchmark_amount"] == 2790.90
    assert "2790.90" in res_11e["reply"]
    print("PASS [Case 11e]: “本次按职工医保结算，保守还是手术？” -> 临床方案选择不误伤已确认险种 ￥2790.90")

    # ---------------- 用例 12: Round 11 审查验收 (暂不更正、更正询问澄清、历史分句隔离、项目自费填充词容错) ----------------
    print("\n--- [Case 12] Round 11 审查验收 (暂不更正、更正询问澄清、历史分句隔离、项目自费填充词容错) ---")
    # 12a. 暂不更正为居民医保 -> 否定前缀'暂不'严禁触发更正，必须保留职工基准 ￥2790.90
    q_12a = "68岁，职工医保，暂不更正为居民医保，M17.9，保守治疗"
    res_12a = parse_local_intent(q_12a)
    assert res_12a["intent"] == "query_dip_rule"
    assert res_12a["data"]["matched"] is True
    assert res_12a["data"]["benchmark_amount"] == 2790.90
    assert "2790.90" in res_12a["reply"]
    print("PASS [Case 12a]: “暂不更正为居民医保” -> 否定前缀识别，保留职工基准 ￥2790.90")

    # 12b. 是否需要更正为居民医保？ -> 疑问形式的更正绝不擅自执行，返回 PENDING / CORRECTION_UNCONFIRMED 澄清
    q_12b = "68岁，职工医保，是否需要更正为居民医保？M17.9，保守治疗"
    res_12b = parse_local_intent(q_12b)
    assert res_12b["intent"] == "need_clarification"
    assert res_12b["data"]["status"] == "PENDING"
    assert res_12b["data"]["pending_reason"] == "CORRECTION_UNCONFIRMED"
    assert "2790.90" not in res_12b["reply"]
    assert "2604.84" not in res_12b["reply"]
    print("PASS [Case 12b]: “是否需要更正为居民医保？” -> 更正询问拦截，PENDING / CORRECTION_UNCONFIRMED，零金额输出")

    # 12c. 原先不确定按职工医保还是居民医保，现已核实按居民医保结算 -> 历史分句(含'原')中的'还是'不得压过当前确认，采用居民基准 ￥2604.84
    q_12c = "68岁，原先不确定按职工医保还是居民医保，现已核实按居民医保结算，M17.9，保守治疗"
    res_12c = parse_local_intent(q_12c)
    assert res_12c["intent"] == "query_dip_rule"
    assert res_12c["data"]["matched"] is True
    assert res_12c["data"]["benchmark_amount"] == 2604.84
    assert "2604.84" in res_12c["reply"]
    print("PASS [Case 12c]: “原先不确定按职工医保还是居民医保，现已核实按居民医保结算” -> 历史分句隔离，采用居民基准 ￥2604.84")

    # 12d. 进口耗材单独按自费项目结算 -> '单独'等填充词不得破坏项目范围识别，整单按职工测算 ￥2790.90
    q_12d = "68岁，职工医保，进口耗材单独按自费项目结算，M17.9，保守治疗"
    res_12d = parse_local_intent(q_12d)
    assert res_12d["intent"] == "query_dip_rule"
    assert res_12d["data"]["matched"] is True
    assert res_12d["data"]["benchmark_amount"] == 2790.90
    assert "2790.90" in res_12d["reply"]
    print("PASS [Case 12d]: “进口耗材单独按自费项目结算” -> 填充词容错，项目级自费隔离，整单按职工测算 ￥2790.90")

    # ---------------- 用例 13: Round 12 审查验收 (整单自费范围优先、句尾'吗'更正询问、多次更正按语序判定) ----------------
    print("\n--- [Case 13] Round 12 审查验收 (整单自费范围优先、句尾'吗'更正询问、多次更正按语序判定) ---")
    # 13a. 登记为职工医保，经检查本次住院按自费结算 -> “本次住院”整单范围词优先于“检查”词语距离，整单自费 NOT_APPLICABLE，零金额输出
    q_13a = "68岁，登记为职工医保，经检查本次住院按自费结算，M17.9，保守治疗"
    res_13a = parse_local_intent(q_13a)
    assert res_13a["intent"] == "query_dip_rule"
    assert res_13a["data"]["status"] == "NOT_APPLICABLE"
    assert "2790.90" not in res_13a["reply"]
    assert "2604.84" not in res_13a["reply"]
    print("PASS [Case 13a]: “登记为职工医保，经检查本次住院按自费结算” -> 整单范围词优先，NOT_APPLICABLE，零金额输出")

    # 13b. 本次按职工医保结算，需要更正为居民医保吗？ -> 句尾'吗'疑问标记识别，更正询问先澄清，绝不擅自执行
    q_13b = "68岁，本次按职工医保结算，需要更正为居民医保吗？M17.9，保守治疗"
    res_13b = parse_local_intent(q_13b)
    assert res_13b["intent"] == "need_clarification"
    assert res_13b["data"]["status"] == "PENDING"
    assert res_13b["data"]["pending_reason"] == "CORRECTION_UNCONFIRMED"
    assert "2790.90" not in res_13b["reply"]
    assert "2604.84" not in res_13b["reply"]
    print("PASS [Case 13b]: “需要更正为居民医保吗？” -> 句尾'吗'疑问识别，PENDING / CORRECTION_UNCONFIRMED，零金额输出")

    # 13c. 已更正为职工医保，是否需要更正为居民医保？ -> 更正询问在已确认更正之后，仍需澄清，严禁静默沿用职工口径
    q_13c = "68岁，已更正为职工医保，是否需要更正为居民医保？M17.9，保守治疗"
    res_13c = parse_local_intent(q_13c)
    assert res_13c["intent"] == "need_clarification"
    assert res_13c["data"]["status"] == "PENDING"
    assert res_13c["data"]["pending_reason"] == "CORRECTION_UNCONFIRMED"
    assert "2790.90" not in res_13c["reply"]
    assert "2604.84" not in res_13c["reply"]
    print("PASS [Case 13c]: “已更正为职工医保，是否需要更正为居民医保？” -> 按语序判定最后更正请求待确认，PENDING / CORRECTION_UNCONFIRMED")

    # ---------------- 用例 14: Round 13 审查验收 (分句问号保留、同分句多次更正定位、整单范围词后项目再收窄) ----------------
    print("\n--- [Case 14] Round 13 审查验收 (分句问号保留、同分句多次更正定位、整单范围词后项目再收窄) ---")
    # 14a. 职工医保，更正为居民医保？ -> 分句句尾问号保留为疑问标记，更正询问先澄清，绝不擅自执行
    q_14a = "68岁，职工医保，更正为居民医保？M17.9，保守治疗"
    res_14a = parse_local_intent(q_14a)
    assert res_14a["intent"] == "need_clarification"
    assert res_14a["data"]["status"] == "PENDING"
    assert res_14a["data"]["pending_reason"] == "CORRECTION_UNCONFIRMED"
    assert "2790.90" not in res_14a["reply"]
    assert "2604.84" not in res_14a["reply"]
    print("PASS [Case 14a]: “更正为居民医保？” -> 分句问号保留为疑问标记，PENDING / CORRECTION_UNCONFIRMED，零金额输出")

    # 14b. 已更正为职工医保后是否需要更正为居民医保？ -> 同一分句内两次更正按(分句, 字符位置)定位，后一次询问待确认
    q_14b = "68岁，已更正为职工医保后是否需要更正为居民医保？M17.9，保守治疗"
    res_14b = parse_local_intent(q_14b)
    assert res_14b["intent"] == "need_clarification"
    assert res_14b["data"]["status"] == "PENDING"
    assert res_14b["data"]["pending_reason"] == "CORRECTION_UNCONFIRMED"
    assert "2790.90" not in res_14b["reply"]
    assert "2604.84" not in res_14b["reply"]
    print("PASS [Case 14b]: “已更正为职工医保后是否需要更正为居民医保？” -> 同分句多次更正按字符位置判定，PENDING / CORRECTION_UNCONFIRMED")

    # 14c. 职工医保，经检查本次住院仅耗材按自费结算 -> 整单范围词后“仅耗材”将自费重新收窄到项目级，保留职工基准 ￥2790.90
    q_14c = "68岁，职工医保，经检查本次住院仅耗材按自费结算，M17.9，保守治疗"
    res_14c = parse_local_intent(q_14c)
    assert res_14c["intent"] == "query_dip_rule"
    assert res_14c["data"]["matched"] is True
    assert res_14c["data"]["benchmark_amount"] == 2790.90
    assert "2790.90" in res_14c["reply"]
    print("PASS [Case 14c]: “经检查本次住院仅耗材按自费结算” -> “仅耗材”收窄为项目级自费，保留职工基准 ￥2790.90")

    # ---------------- 用例 15: DIP-INS-001 验收 (条件性更正不生效：待核实/如果则 先澄清，已确认更正对照执行) ----------------
    print("\n--- [Case 15] DIP-INS-001 验收 (条件性更正不生效：待核实/如果则 先澄清，已确认更正对照执行) ---")
    # 15a. 本次按职工医保结算，待核实后再更正为居民医保 -> 前提“待核实”未满足，更正未生效，先澄清并省略金额
    q_15a = "68岁，本次按职工医保结算，待核实后再更正为居民医保，M17.9，保守治疗"
    res_15a = parse_local_intent(q_15a)
    assert res_15a["intent"] == "need_clarification"
    assert res_15a["data"]["status"] == "PENDING"
    assert res_15a["data"]["pending_reason"] == "CORRECTION_UNCONFIRMED"
    assert "2790.90" not in res_15a["reply"]
    assert "2604.84" not in res_15a["reply"]
    print("PASS [Case 15a]: “待核实后再更正为居民医保” -> 条件未满足不生效，PENDING / CORRECTION_UNCONFIRMED，零金额输出")

    # 15b. 本次职工医保，如果符合条件则更正为居民医保 -> “如果…则”条件句，更正未生效，先澄清并省略金额
    q_15b = "68岁，本次职工医保，如果符合条件则更正为居民医保，M17.9，保守治疗"
    res_15b = parse_local_intent(q_15b)
    assert res_15b["intent"] == "need_clarification"
    assert res_15b["data"]["status"] == "PENDING"
    assert res_15b["data"]["pending_reason"] == "CORRECTION_UNCONFIRMED"
    assert "2790.90" not in res_15b["reply"]
    assert "2604.84" not in res_15b["reply"]
    print("PASS [Case 15b]: “如果符合条件则更正为居民医保” -> 条件句不生效，PENDING / CORRECTION_UNCONFIRMED，零金额输出")

    # 15c. 对照例：本次按职工医保结算，已核实并确认更正为居民医保 -> 已确认更正正常执行，采用居民基准 ￥2604.84
    q_15c = "68岁，本次按职工医保结算，已核实并确认更正为居民医保，M17.9，保守治疗"
    res_15c = parse_local_intent(q_15c)
    assert res_15c["intent"] == "query_dip_rule"
    assert res_15c["data"]["matched"] is True
    assert res_15c["data"]["benchmark_amount"] == 2604.84
    assert "2604.84" in res_15c["reply"]
    print("PASS [Case 15c]: “已核实并确认更正为居民医保” -> 对照例：已确认更正正常执行，居民基准 ￥2604.84")

    # ---------------- 用例 16: DIP-INS-001/002 复审验收 (跨分句未决前提关联更正、前提已落实的确认更正放行) ----------------
    print("\n--- [Case 16] DIP-INS-001/002 复审验收 (跨分句未决前提关联更正、前提已落实的确认更正放行) ---")
    # 16a. 本次按职工医保结算，如果审批通过，再更正为居民医保 -> “如果审批通过”跨分句修饰下一条更正，前提未决，先澄清并省略金额
    q_16a = "68岁，本次按职工医保结算，如果审批通过，再更正为居民医保，M17.9，保守治疗"
    res_16a = parse_local_intent(q_16a)
    assert res_16a["intent"] == "need_clarification"
    assert res_16a["data"]["status"] == "PENDING"
    assert res_16a["data"]["pending_reason"] == "CORRECTION_UNCONFIRMED"
    assert "2790.90" not in res_16a["reply"]
    assert "2604.84" not in res_16a["reply"]
    print("PASS [Case 16a]: “如果审批通过，再更正为居民医保” -> 跨分句未决前提关联更正，PENDING / CORRECTION_UNCONFIRMED，零金额输出")

    # 16b. 对照例：本次按职工医保结算，已核实符合条件并确认更正为居民医保 -> “符合条件”前提已被“已核实/确认”落实，更正生效，采用居民基准 ￥2604.84
    q_16b = "68岁，本次按职工医保结算，已核实符合条件并确认更正为居民医保，M17.9，保守治疗"
    res_16b = parse_local_intent(q_16b)
    assert res_16b["intent"] == "query_dip_rule"
    assert res_16b["data"]["matched"] is True
    assert res_16b["data"]["benchmark_amount"] == 2604.84
    assert "2604.84" in res_16b["reply"]
    print("PASS [Case 16b]: “已核实符合条件并确认更正为居民医保” -> 前提已落实，确认更正放行，居民基准 ￥2604.84")

    # ---------------- 用例 17: DIP-INS-001/002 新变体验收 (假设句内完成态词不放行；后续分句确认前提则更正生效) ----------------
    print("\n--- [Case 17] DIP-INS-001/002 新变体验收 (假设句完成态词误判修复：如果已获批则→待定；现已确认审批通过→更正生效) ---")
    # 17a. 本次按职工医保结算，如果已获批则更正为居民医保 -> “已获批”位于“如果…则”假设句内，只是假设前提而非事实确认，
    #      更正未生效，先澄清并省略金额（DIP-INS-001 新变体）
    q_17a = "68岁，本次按职工医保结算，如果已获批则更正为居民医保，M17.9，保守治疗"
    res_17a = parse_local_intent(q_17a)
    assert res_17a["intent"] == "need_clarification"
    assert res_17a["data"]["status"] == "PENDING"
    assert res_17a["data"]["pending_reason"] == "CORRECTION_UNCONFIRMED"
    assert "2790.90" not in res_17a["reply"]
    assert "2604.84" not in res_17a["reply"]
    print("PASS [Case 17a]: “如果已获批则更正为居民医保” -> 假设句内完成态词不放行，PENDING / CORRECTION_UNCONFIRMED，零金额输出")

    # 17b. 对照例：本次职工医保，如果审批通过，现已确认审批通过并更正为居民医保 -> 后续分句以陈述事实口吻
    #      明确确认同一前提结果（“现已确认审批通过”），解除跨分句待定，更正生效，采用居民基准 ￥2604.84（DIP-INS-002 新变体）
    q_17b = "68岁，本次职工医保，如果审批通过，现已确认审批通过并更正为居民医保，M17.9，保守治疗"
    res_17b = parse_local_intent(q_17b)
    assert res_17b["intent"] == "query_dip_rule"
    assert res_17b["data"]["matched"] is True
    assert res_17b["data"]["benchmark_amount"] == 2604.84
    assert "2604.84" in res_17b["reply"]
    print("PASS [Case 17b]: “如果审批通过，现已确认审批通过并更正为居民医保” -> 前提结果被确认，更正生效，居民基准 ￥2604.84")

    # ---------------- 用例 18: DIP-INS-001 变体三验收 (确认内容须针对同一前提：核实年龄≠审批通过) ----------------
    print("\n--- [Case 18] DIP-INS-001 变体三验收 (无关确认不解除待定：已核实年龄 ≠ 审批通过；确认审批通过对照放行) ---")
    # 18a. 本次职工医保，如果审批通过，已核实年龄并更正为居民医保 -> “已核实年龄”确认的是年龄而非审批前提，
    #      待定绝不解除，先澄清并省略金额（DIP-INS-001 变体）
    q_18a = "68岁，本次职工医保，如果审批通过，已核实年龄并更正为居民医保，M17.9，保守治疗"
    res_18a = parse_local_intent(q_18a)
    assert res_18a["intent"] == "need_clarification"
    assert res_18a["data"]["status"] == "PENDING"
    assert res_18a["data"]["pending_reason"] == "CORRECTION_UNCONFIRMED"
    assert "2790.90" not in res_18a["reply"]
    assert "2604.84" not in res_18a["reply"]
    print("PASS [Case 18a]: “如果审批通过，已核实年龄并更正为居民医保” -> 无关确认不解除待定，PENDING / CORRECTION_UNCONFIRMED，零金额输出")

    # 18b. 对照例：本次职工医保，如果审批通过，已确认审批通过并更正为居民医保 -> 确认内容正是审批前提本身，
    #      解除待定，更正生效，采用居民基准 ￥2604.84
    q_18b = "68岁，本次职工医保，如果审批通过，已确认审批通过并更正为居民医保，M17.9，保守治疗"
    res_18b = parse_local_intent(q_18b)
    assert res_18b["intent"] == "query_dip_rule"
    assert res_18b["data"]["matched"] is True
    assert res_18b["data"]["benchmark_amount"] == 2604.84
    assert "2604.84" in res_18b["reply"]
    print("PASS [Case 18b]: “如果审批通过，已确认审批通过并更正为居民医保” -> 确认同一前提，更正生效，居民基准 ￥2604.84")

    # ---------------- 用例 19: DIP-INS-001 变体四验收 (确认须同时覆盖前提事项与结果词) ----------------
    print("\n--- [Case 19] DIP-INS-001 变体四验收 (“尚在办理/材料齐全”非审批结果，绝不解除待定) ---")
    # 19a. “已确认审批尚在办理”——事项相关但无肯定结果词（通过），更正不生效，先澄清并省略金额
    q_19a = "68岁，本次职工医保，如果审批通过，已确认审批尚在办理并更正为居民医保，M17.9，保守治疗"
    res_19a = parse_local_intent(q_19a)
    assert res_19a["intent"] == "need_clarification"
    assert res_19a["data"]["status"] == "PENDING"
    assert res_19a["data"]["pending_reason"] == "CORRECTION_UNCONFIRMED"
    assert "2790.90" not in res_19a["reply"]
    assert "2604.84" not in res_19a["reply"]
    print("PASS [Case 19a]: “如果审批通过，已确认审批尚在办理并更正为居民医保” -> 事项在办理非结果，PENDING / CORRECTION_UNCONFIRMED，零金额输出")

    # 19b. “已核实审批材料齐全”——材料齐全不等于审批通过，更正不生效，先澄清并省略金额；
    #      对照仍由 Case 18b（“已确认审批通过”正常放行）覆盖
    q_19b = "68岁，本次职工医保，如果审批通过，已核实审批材料齐全并更正为居民医保，M17.9，保守治疗"
    res_19b = parse_local_intent(q_19b)
    assert res_19b["intent"] == "need_clarification"
    assert res_19b["data"]["status"] == "PENDING"
    assert res_19b["data"]["pending_reason"] == "CORRECTION_UNCONFIRMED"
    assert "2790.90" not in res_19b["reply"]
    assert "2604.84" not in res_19b["reply"]
    print("PASS [Case 19b]: “如果审批通过，已核实审批材料齐全并更正为居民医保” -> 材料齐全非审批结果，PENDING / CORRECTION_UNCONFIRMED，零金额输出")

    # ---------------- 用例 20: DIP-INS-001 变体五验收 (确认须同话题且结果为肯定) ----------------
    print("\n--- [Case 20] DIP-INS-001 变体五验收 (“审批未通过”否定结果与“身份核验”无关事项，绝不解除待定) ---")
    # 20a. 本次职工医保，如果审批通过，已核实审批未通过并更正为居民医保 -> “未通过”是前提的否定结果，
    #      前提未满足且结果相反，更正绝不生效，先澄清并省略金额
    q_20a = "68岁，本次职工医保，如果审批通过，已核实审批未通过并更正为居民医保，M17.9，保守治疗"
    res_20a = parse_local_intent(q_20a)
    assert res_20a["intent"] == "need_clarification"
    assert res_20a["data"]["status"] == "PENDING"
    assert res_20a["data"]["pending_reason"] == "CORRECTION_UNCONFIRMED"
    assert "2790.90" not in res_20a["reply"]
    assert "2604.84" not in res_20a["reply"]
    print("PASS [Case 20a]: “如果审批通过，已核实审批未通过并更正为居民医保” -> 否定结果不解除待定，PENDING / CORRECTION_UNCONFIRMED，零金额输出")

    # 20b. 本次职工医保，如果审批通过，已确认身份核验通过并更正为居民医保 -> “身份核验通过”与审批前提无关，
    #      更正绝不生效，先澄清并省略金额；正对照仍由 Case 18b（“已确认审批通过”放行 ￥2604.84）覆盖
    q_20b = "68岁，本次职工医保，如果审批通过，已确认身份核验通过并更正为居民医保，M17.9，保守治疗"
    res_20b = parse_local_intent(q_20b)
    assert res_20b["intent"] == "need_clarification"
    assert res_20b["data"]["status"] == "PENDING"
    assert res_20b["data"]["pending_reason"] == "CORRECTION_UNCONFIRMED"
    assert "2790.90" not in res_20b["reply"]
    assert "2604.84" not in res_20b["reply"]
    print("PASS [Case 20b]: “如果审批通过，已确认身份核验通过并更正为居民医保” -> 无关事项确认不解除待定，PENDING / CORRECTION_UNCONFIRMED，零金额输出")

    # ---------------- 用例 21: DIP-INS-001 变体六验收 (较长否定形式与阶段性结果均不解除待定) ----------------
    print("\n--- [Case 21] DIP-INS-001 变体六验收 (“审批没有通过”较长否定、“通过初审但仍待终审”阶段性结果，绝不解除待定) ---")
    # 21a. 本次职工医保，如果审批通过，已核实审批没有通过并更正为居民医保 -> “没有通过”是前提的否定结果，
    #      与“未通过”等义，更正绝不生效，先澄清并省略金额
    q_21a = "68岁，本次职工医保，如果审批通过，已核实审批没有通过并更正为居民医保，M17.9，保守治疗"
    res_21a = parse_local_intent(q_21a)
    assert res_21a["intent"] == "need_clarification"
    assert res_21a["data"]["status"] == "PENDING"
    assert res_21a["data"]["pending_reason"] == "CORRECTION_UNCONFIRMED"
    assert "2790.90" not in res_21a["reply"]
    assert "2604.84" not in res_21a["reply"]
    print("PASS [Case 21a]: “如果审批通过，已核实审批没有通过并更正为居民医保” -> 较长否定不解除待定，PENDING / CORRECTION_UNCONFIRMED，零金额输出")

    # 21b. 本次职工医保，如果审批通过，已确认审批材料通过初审但仍待终审并更正为居民医保 -> “通过初审”只是阶段性结果，
    #      “仍待终审”表明前提未最终满足，更正绝不生效，先澄清并省略金额；
    #      正对照仍由 Case 18b（“已确认审批通过”放行 ￥2604.84）覆盖
    q_21b = "68岁，本次职工医保，如果审批通过，已确认审批材料通过初审但仍待终审并更正为居民医保，M17.9，保守治疗"
    res_21b = parse_local_intent(q_21b)
    assert res_21b["intent"] == "need_clarification"
    assert res_21b["data"]["status"] == "PENDING"
    assert res_21b["data"]["pending_reason"] == "CORRECTION_UNCONFIRMED"
    assert "2790.90" not in res_21b["reply"]
    assert "2604.84" not in res_21b["reply"]
    print("PASS [Case 21b]: “如果审批通过，已确认审批材料通过初审但仍待终审并更正为居民医保” -> 阶段性结果不解除待定，PENDING / CORRECTION_UNCONFIRMED，零金额输出")

    # ---------------- 用例 22: DIP-INS-001 变体七验收 (部分限定/疑问未决/否决表述均不解除待定，聊天与结构化双路径) ----------------
    print("\n--- [Case 22] DIP-INS-001 变体七验收 (“仅通过初审”“是否通过尚无结论”“不予通过”，双路径均不解除待定) ---")
    # 三种审批表述：部分限定（仅通过初审）、疑问未决（是否通过尚无结论）、否决（不予通过），
    # 均非同一前提的最终肯定结果，更正绝不生效；聊天入口(parse_local_intent)与结构化入口(match_dip_rule)同时覆盖
    v7_variants = [
        ("22a", "审批仅通过初审"),
        ("22b", "审批是否通过尚无结论"),
        ("22c", "审批结果为不予通过"),
    ]
    for tag, stage_text in v7_variants:
        q = f"68岁，本次职工医保，如果审批通过，已确认{stage_text}并更正为居民医保，M17.9，保守治疗"
        # 路径 1：聊天入口
        res_c = parse_local_intent(q)
        assert res_c["intent"] == "need_clarification", f"Case {tag} 聊天入口应澄清，实际 {res_c['intent']}"
        assert res_c["data"]["status"] == "PENDING"
        assert res_c["data"]["pending_reason"] == "CORRECTION_UNCONFIRMED"
        assert "2790.90" not in res_c["reply"] and "2604.84" not in res_c["reply"]
        # 路径 2：结构化入口（同一险种表述原样传入）
        res_s = match_dip_rule(disease="M17.9", age=68,
                               ins_type=f"本次职工医保，如果审批通过，已确认{stage_text}并更正为居民医保",
                               op_keyword="保守")
        assert res_s["matched"] is False, f"Case {tag} 结构化入口应不命中，实际 {res_s}"
        assert res_s["status"] == "PENDING"
        assert res_s["pending_reason"] == "CORRECTION_UNCONFIRMED"
        assert "benchmark_amount" not in res_s
        print(f"PASS [Case {tag}]: “已确认{stage_text}并更正为居民医保” -> 双路径均 PENDING / CORRECTION_UNCONFIRMED，零金额输出")

    # 22d. 正对照：同一前提的最终肯定结果（审批通过）正常放行，居民基准 ￥2604.84（双路径）
    q_22d = "68岁，本次职工医保，如果审批通过，已确认审批通过并更正为居民医保，M17.9，保守治疗"
    res_22d = parse_local_intent(q_22d)
    assert res_22d["intent"] == "query_dip_rule"
    assert res_22d["data"]["matched"] is True
    assert res_22d["data"]["benchmark_amount"] == 2604.84
    res_22d_s = match_dip_rule(disease="M17.9", age=68,
                               ins_type="本次职工医保，如果审批通过，已确认审批通过并更正为居民医保",
                               op_keyword="保守")
    assert res_22d_s["matched"] is True
    assert res_22d_s["benchmark_amount"] == 2604.84
    print("PASS [Case 22d]: “已确认审批通过并更正为居民医保” -> 正对照双路径放行，居民基准 ￥2604.84")

    # ---------------- 用例 23: DIP-INS-001 变体八验收 (终审否决/初审阶段/辟谣撤回，双路径均不解除待定) ----------------
    print("\n--- [Case 23] DIP-INS-001 变体八验收 (“审批终审未通过但初审通过”“审批初审通过”“审批通过的消息有误”，双路径表驱动) ---")
    v8_variants = [
        ("23a", "审批终审未通过但初审通过"),
        ("23b", "审批初审通过"),
        ("23c", "审批通过的消息有误"),
        ("23d", "审批通过的消息不实"),
    ]
    for tag, stage_text in v8_variants:
        q = f"68岁，本次职工医保，如果审批通过，已确认{stage_text}并更正为居民医保，M17.9，保守治疗"
        # 路径 1：聊天入口
        res_c = parse_local_intent(q)
        assert res_c["intent"] == "need_clarification", f"Case {tag} 聊天入口应澄清，实际 {res_c['intent']}"
        assert res_c["data"]["status"] == "PENDING"
        assert res_c["data"]["pending_reason"] == "CORRECTION_UNCONFIRMED"
        assert "2790.90" not in res_c["reply"] and "2604.84" not in res_c["reply"]
        # 路径 2：结构化入口（同一险种表述原样传入）
        res_s = match_dip_rule(disease="M17.9", age=68,
                               ins_type=f"本次职工医保，如果审批通过，已确认{stage_text}并更正为居民医保",
                               op_keyword="保守")
        assert res_s["matched"] is False, f"Case {tag} 结构化入口应不命中，实际 {res_s}"
        assert res_s["status"] == "PENDING"
        assert res_s["pending_reason"] == "CORRECTION_UNCONFIRMED"
        assert "benchmark_amount" not in res_s
        print(f"PASS [Case {tag}]: “已确认{stage_text}并更正为居民医保” -> 双路径均 PENDING / CORRECTION_UNCONFIRMED，零金额输出")

    # 23e. 正对照：同一前提的最终肯定结果（审批通过）正常放行，居民基准 ￥2604.84（双路径）
    q_23e = "68岁，本次职工医保，如果审批通过，已确认审批通过并更正为居民医保，M17.9，保守治疗"
    res_23e = parse_local_intent(q_23e)
    assert res_23e["intent"] == "query_dip_rule"
    assert res_23e["data"]["matched"] is True
    assert res_23e["data"]["benchmark_amount"] == 2604.84
    res_23e_s = match_dip_rule(disease="M17.9", age=68,
                               ins_type="本次职工医保，如果审批通过，已确认审批通过并更正为居民医保",
                               op_keyword="保守")
    assert res_23e_s["matched"] is True
    assert res_23e_s["benchmark_amount"] == 2604.84
    print("PASS [Case 23e]: “已确认审批通过并更正为居民医保” -> 正对照双路径放行，居民基准 ￥2604.84")

    # ---------------- 用例 24: DIP-INS-001 变体九验收 (无关审批/审核事项严禁误放行，双路径表驱动) ----------------
    print("\n--- [Case 24] DIP-INS-001 变体九验收 (“排班审核通过”“费用审核通过”“采购审批通过”，双路径表驱动) ---")
    v9_variants = [
        ("24a", "排班审核通过"),
        ("24b", "费用审核通过"),
        ("24c", "采购审批通过"),
    ]
    for tag, stage_text in v9_variants:
        q = f"68岁，本次职工医保，如果审批通过，已确认{stage_text}并更正为居民医保，M17.9，保守治疗"
        res_c = parse_local_intent(q)
        assert res_c["intent"] == "need_clarification", f"Case {tag} 聊天入口应澄清，实际 {res_c['intent']}"
        assert res_c["data"]["status"] == "PENDING"
        assert res_c["data"]["pending_reason"] == "CORRECTION_UNCONFIRMED"
        assert "2790.90" not in res_c["reply"] and "2604.84" not in res_c["reply"]
        res_s = match_dip_rule(disease="M17.9", age=68,
                               ins_type=f"本次职工医保，如果审批通过，已确认{stage_text}并更正为居民医保",
                               op_keyword="保守")
        assert res_s["matched"] is False, f"Case {tag} 结构化入口应不命中，实际 {res_s}"
        assert res_s["status"] == "PENDING"
        assert res_s["pending_reason"] == "CORRECTION_UNCONFIRMED"
        assert "benchmark_amount" not in res_s
        print(f"PASS [Case {tag}]: “已确认{stage_text}并更正为居民医保” -> 双路径均 PENDING / CORRECTION_UNCONFIRMED，零金额输出")

    # 24d. 正对照：同一前提的最终肯定结果（审批通过）正常放行，居民基准 ￥2604.84（双路径）
    q_24d = "68岁，本次职工医保，如果审批通过，已确认审批通过并更正为居民医保，M17.9，保守治疗"
    res_24d = parse_local_intent(q_24d)
    assert res_24d["intent"] == "query_dip_rule"
    assert res_24d["data"]["matched"] is True
    assert res_24d["data"]["benchmark_amount"] == 2604.84
    res_24d_s = match_dip_rule(disease="M17.9", age=68,
                               ins_type="本次职工医保，如果审批通过，已确认审批通过并更正为居民医保",
                               op_keyword="保守")
    assert res_24d_s["matched"] is True
    assert res_24d_s["benchmark_amount"] == 2604.84
    print("PASS [Case 24d]: “已确认审批通过并更正为居民医保” -> 正对照双路径放行，居民基准 ￥2604.84")

    # ---------------- 用例 25: DIP-INS-001 变体十验收 (“手术审批通过”“采购申请审批通过”等无关主语审批严禁放行) ----------------
    print("\n--- [Case 25] DIP-INS-001 变体十验收 (“手术审批通过”“采购申请审批通过”“费用审核通过”，双路径表驱动) ---")
    v10_variants = [
        ("25a", "手术审批通过"),
        ("25b", "采购申请审批通过"),
        ("25c", "费用审核通过"),
    ]
    for tag, stage_text in v10_variants:
        q = f"68岁，本次职工医保，如果审批通过，已确认{stage_text}并更正为居民医保，M17.9，保守治疗"
        res_c = parse_local_intent(q)
        assert res_c["intent"] == "need_clarification", f"Case {tag} 聊天入口应澄清，实际 {res_c['intent']}"
        assert res_c["data"]["status"] == "PENDING"
        assert res_c["data"]["pending_reason"] == "CORRECTION_UNCONFIRMED"
        assert "2790.90" not in res_c["reply"] and "2604.84" not in res_c["reply"]
        res_s = match_dip_rule(disease="M17.9", age=68,
                               ins_type=f"本次职工医保，如果审批通过，已确认{stage_text}并更正为居民医保",
                               op_keyword="保守")
        assert res_s["matched"] is False
        assert res_s["status"] == "PENDING"
        assert res_s["pending_reason"] == "CORRECTION_UNCONFIRMED"
        assert "benchmark_amount" not in res_s
        print(f"PASS [Case {tag}]: “已确认{stage_text}并更正为居民医保” -> 双路径均 PENDING / CORRECTION_UNCONFIRMED，零金额输出")

    q_25d = "68岁，本次职工医保，如果审批通过，已确认审批通过并更正为居民医保，M17.9，保守治疗"
    res_25d = parse_local_intent(q_25d)
    assert res_25d["intent"] == "query_dip_rule" and res_25d["data"]["benchmark_amount"] == 2604.84
    res_25d_s = match_dip_rule(disease="M17.9", age=68,
                               ins_type="本次职工医保，如果审批通过，已确认审批通过并更正为居民医保",
                               op_keyword="保守")
    assert res_25d_s["matched"] is True and res_25d_s["benchmark_amount"] == 2604.84
    print("PASS [Case 25d]: “已确认审批通过并更正为居民医保” -> 正对照双路径放行，居民基准 ￥2604.84")

    # ---------------- 用例 26: DIP-INS-001 变体十一验收 (“预计明天通过”“计划通过”预期结果严禁放行) ----------------
    print("\n--- [Case 26] DIP-INS-001 变体十一验收 (“审批预计明天通过”“审批计划通过”与肯定对照，双路径表驱动) ---")
    v11_variants = [
        ("26a", "审批预计明天通过"),
        ("26b", "审批计划通过"),
    ]
    for tag, stage_text in v11_variants:
        q = f"68岁，本次职工医保，如果审批通过，已确认{stage_text}并更正为居民医保，M17.9，保守治疗"
        res_c = parse_local_intent(q)
        assert res_c["intent"] == "need_clarification", f"Case {tag} 聊天入口应澄清，实际 {res_c['intent']}"
        assert res_c["data"]["status"] == "PENDING"
        assert res_c["data"]["pending_reason"] == "CORRECTION_UNCONFIRMED"
        assert "2790.90" not in res_c["reply"] and "2604.84" not in res_c["reply"]
        res_s = match_dip_rule(disease="M17.9", age=68,
                               ins_type=f"本次职工医保，如果审批通过，已确认{stage_text}并更正为居民医保",
                               op_keyword="保守")
        assert res_s["matched"] is False
        assert res_s["status"] == "PENDING"
        assert res_s["pending_reason"] == "CORRECTION_UNCONFIRMED"
        assert "benchmark_amount" not in res_s
        print(f"PASS [Case {tag}]: “已确认{stage_text}并更正为居民医保” -> 双路径均 PENDING / CORRECTION_UNCONFIRMED，零金额输出")

    q_26c = "68岁，本次职工医保，如果审批通过，已确认审批通过并更正为居民医保，M17.9，保守治疗"
    res_26c = parse_local_intent(q_26c)
    assert res_26c["intent"] == "query_dip_rule" and res_26c["data"]["benchmark_amount"] == 2604.84
    res_26c_s = match_dip_rule(disease="M17.9", age=68,
                               ins_type="本次职工医保，如果审批通过，已确认审批通过并更正为居民医保",
                               op_keyword="保守")
    assert res_26c_s["matched"] is True and res_26c_s["benchmark_amount"] == 2604.84
    print("PASS [Case 26c]: “已确认审批通过并更正为居民医保” -> 正对照双路径放行，居民基准 ￥2604.84")

    # ---------------- 用例 27: DIP-INS-001 变体十二验收 (“可能通过”“有机会通过”推测表述拦截与“已于上周通过”对照) ----------------
    print("\n--- [Case 27] DIP-INS-001 变体十二验收 (4 个未决用例 + 2 个已完成对照，双路径表驱动) ---")
    v12_pending = [
        ("27a", "审批预计明天通过"),
        ("27b", "审批计划通过"),
        ("27c", "审批可能通过"),
        ("27d", "审批有机会通过"),
    ]
    for tag, stage_text in v12_pending:
        q = f"68岁，本次职工医保，如果审批通过，已确认{stage_text}并更正为居民医保，M17.9，保守治疗"
        res_c = parse_local_intent(q)
        assert res_c["intent"] == "need_clarification", f"Case {tag} 聊天入口应澄清，实际 {res_c['intent']}"
        assert res_c["data"]["status"] == "PENDING"
        assert res_c["data"]["pending_reason"] == "CORRECTION_UNCONFIRMED"
        assert "2790.90" not in res_c["reply"] and "2604.84" not in res_c["reply"]
        res_s = match_dip_rule(disease="M17.9", age=68,
                               ins_type=f"本次职工医保，如果审批通过，已确认{stage_text}并更正为居民医保",
                               op_keyword="保守")
        assert res_s["matched"] is False
        assert res_s["status"] == "PENDING"
        assert res_s["pending_reason"] == "CORRECTION_UNCONFIRMED"
        assert "benchmark_amount" not in res_s
        print(f"PASS [Case {tag}]: “已确认{stage_text}并更正为居民医保” -> 双路径均 PENDING / CORRECTION_UNCONFIRMED，零金额输出")

    for tag, ok_text in [("27e", "审批通过"), ("27f", "审批已于上周通过")]:
        q_ok = f"68岁，本次职工医保，如果审批通过，已确认{ok_text}并更正为居民医保，M17.9，保守治疗"
        res_ok_c = parse_local_intent(q_ok)
        assert res_ok_c["intent"] == "query_dip_rule" and res_ok_c["data"]["benchmark_amount"] == 2604.84
        res_ok_s = match_dip_rule(disease="M17.9", age=68,
                                  ins_type=f"本次职工医保，如果审批通过，已确认{ok_text}并更正为居民医保",
                                  op_keyword="保守")
        assert res_ok_s["matched"] is True and res_ok_s["benchmark_amount"] == 2604.84
        print(f"PASS [Case {tag}]: “已确认{ok_text}并更正为居民医保” -> 正对照双路径放行，居民基准 ￥2604.84")

    # ---------------- 用例 28: DIP-INS-001 变体十三验收 (未知/疑问/生效条件拦截 + 过去时间约数“大约三天前”放行) ----------------
    print("\n--- [Case 28] DIP-INS-001 变体十三验收 (未知/疑问/生效条件 vs 过去时间约数，双路径表驱动) ---")
    v13_pending = [
        ("28a", "审批通过与否还不知道"),
        ("28b", "审批结果尚不清楚能否通过"),
        ("28c", "审批须通过后方可生效"),
    ]
    for tag, stage_text in v13_pending:
        q = f"68岁，本次职工医保，如果审批通过，已确认{stage_text}并更正为居民医保，M17.9，保守治疗"
        res_c = parse_local_intent(q)
        assert res_c["intent"] == "need_clarification", f"Case {tag} 聊天入口应澄清，实际 {res_c['intent']}"
        assert res_c["data"]["status"] == "PENDING"
        assert res_c["data"]["pending_reason"] == "CORRECTION_UNCONFIRMED"
        assert "2790.90" not in res_c["reply"] and "2604.84" not in res_c["reply"]
        res_s = match_dip_rule(disease="M17.9", age=68,
                               ins_type=f"本次职工医保，如果审批通过，已确认{stage_text}并更正为居民医保",
                               op_keyword="保守")
        assert res_s["matched"] is False
        assert res_s["status"] == "PENDING"
        assert res_s["pending_reason"] == "CORRECTION_UNCONFIRMED"
        assert "benchmark_amount" not in res_s
        print(f"PASS [Case {tag}]: “已确认{stage_text}并更正为居民医保” -> 双路径均 PENDING / CORRECTION_UNCONFIRMED，零金额输出")

    for tag, ok_text in [("28d", "审批大约三天前已经通过"), ("28e", "审批已于上周通过")]:
        q_ok = f"68岁，本次职工医保，如果审批通过，已确认{ok_text}并更正为居民医保，M17.9，保守治疗"
        res_ok_c = parse_local_intent(q_ok)
        assert res_ok_c["intent"] == "query_dip_rule" and res_ok_c["data"]["benchmark_amount"] == 2604.84
        res_ok_s = match_dip_rule(disease="M17.9", age=68,
                                  ins_type=f"本次职工医保，如果审批通过，已确认{ok_text}并更正为居民医保",
                                  op_keyword="保守")
        assert res_ok_s["matched"] is True and res_ok_s["benchmark_amount"] == 2604.84
        print(f"PASS [Case {tag}]: “已确认{ok_text}并更正为居民医保” -> 正对照双路径放行，居民基准 ￥2604.84")

    print("\n" + "=" * 70)
    print(">>> 恭喜！Phase 1 验收回归测试套件全部 28 大关键测试集 100% 通过！ <<<")
    print("=" * 70)

if __name__ == "__main__":
    run_phase1_tests()

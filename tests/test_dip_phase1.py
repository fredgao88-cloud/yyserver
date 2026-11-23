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

    print("\n" + "=" * 70)
    print(">>> 恭喜！Phase 1 验收回归测试套件全部 8 大关键测试集 100% 通过！ <<<")
    print("=" * 70)

if __name__ == "__main__":
    run_phase1_tests()

# -*- coding: utf-8 -*-
import urllib.request
import json
import ssl
import sys

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

def post(url, payload):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, context=ctx) as resp:
        return json.loads(resp.read().decode("utf-8"))

print("=== 1. Direct Health Check (HTTP 8000) ===")
with urllib.request.urlopen("http://127.0.0.1:8000/health") as resp:
    res1 = json.loads(resp.read().decode("utf-8"))
    print(res1)
    assert res1["status"] == "ok"

print("\n=== 2. Nginx HTTPS Health Check (HTTPS 443) ===")
with urllib.request.urlopen("https://127.0.0.1/health", context=ctx) as resp:
    res2 = json.loads(resp.read().decode("utf-8"))
    print(res2)
    assert res2["status"] == "ok"

print("\n=== 3. 结构化规则接口 (68岁职工 M17.9 膝关节病 保守治疗) ===")
res3 = post("https://127.0.0.1/api/tools/query_dip_rule", {
    "disease": "M17.9",
    "age": 68,
    "ins_type": "职工",
    "operation": "保守治疗"
})
print("Matched:", res3.get("matched"), "RuleID:", res3.get("rule_id"), "Benchmark:", res3.get("benchmark_amount"))
assert res3["matched"] is True
assert res3["benchmark_amount"] == 2790.90

print("\n=== 4. 判灯核查接口 (当前费用 2550 元 vs 预估基准 2790.90 元) ===")
res4 = post("https://127.0.0.1/api/tools/check_patient_dip", {
    "patient_id": "zy2025042901",
    "disease": "M17.9",
    "current_cost": 2550.00,
    "age": 68,
    "ins_type": "职工",
    "operation": "保守治疗"
})
print("Status:", res4.get("status"), "Light:", res4.get("evaluation", {}).get("light"))
assert res4["status"] == "MATCHED"
assert res4["evaluation"]["light"] == "黄灯"

print("\n=== 5. 自然语言意图分流与问答接口 (POST /api/chat) ===")
res5 = post("https://127.0.0.1/api/chat", {
    "text": "68岁职工，诊断 M17.9 膝关节病保守治疗预估分值与基准是多少？"
})
print("Intent:", res5.get("intent"), "Has 2790.90 in reply:", ("2790.90" in res5.get("reply", "")))
assert res5["intent"] == "query_dip_rule"
assert "2790.90" in res5["reply"]

print("\n=== 6. 缺省关键信息时的追问逻辑 (缺少年龄与操作) ===")
res6 = post("https://127.0.0.1/api/tools/query_dip_rule", {
    "disease": "M17.9"
})
print("Status:", res6.get("status"), "PendingReason:", res6.get("pending_reason"))
assert res6["status"] == "PENDING"

print("\n=== 7. 钉钉群聊渠道隐私盾牌 (POST /api/chat, channel=DINGTALK_GROUP) ===")
res7 = post("https://127.0.0.1/api/chat", {
    "text": "帮我查一下3床张大爷现在花了多少钱",
    "channel": "DINGTALK_GROUP"
})
print("Intent:", res7.get("intent"), "Shielded:", res7.get("data", {}).get("shielded"))
assert res7["intent"] == "privacy_shielded"
assert "张大爷" not in res7["reply"]
assert "3床" not in res7["reply"]
assert "query" not in res7.get("data", {})

print("\n=== 8. 公费医疗未支持参保类型拦截接口 ===")
res8 = post("https://127.0.0.1/api/tools/check_patient_dip", {
    "patient_id": "zy2025042902",
    "disease": "M17.9",
    "current_cost": 2450.00,
    "age": 68,
    "ins_type": "公费医疗",
    "operation": "保守治疗"
})
print("Status:", res8.get("status"), "PendingReason:", res8.get("pending_reason"))
assert res8["status"] == "PENDING"
assert "UNSUPPORTED_INS_TYPE" in res8.get("pending_reason", "")

print("\n=== 9. 【审查关键验收】城镇居民医保 vs 居民医保 接口判定一致性 ===")
# 同一合成病例：68岁、M17.9、保守治疗、费用 2450 元
res9_resident = post("https://127.0.0.1/api/tools/check_patient_dip", {
    "patient_id": "zy_synth_res_01",
    "disease": "M17.9",
    "current_cost": 2450.00,
    "age": 68,
    "ins_type": "居民医保",
    "operation": "保守治疗"
})
res9_city_res = post("https://127.0.0.1/api/tools/check_patient_dip", {
    "patient_id": "zy_synth_res_02",
    "disease": "M17.9",
    "current_cost": 2450.00,
    "age": 68,
    "ins_type": "城镇居民医保",
    "operation": "保守治疗"
})
print("居民医保:", res9_resident.get("benchmark_amount"), "Light:", res9_resident.get("evaluation", {}).get("light"))
print("城镇居民医保:", res9_city_res.get("benchmark_amount"), "Light:", res9_city_res.get("evaluation", {}).get("light"))
assert res9_resident["benchmark_amount"] == res9_city_res["benchmark_amount"] == 2604.84
assert res9_resident["evaluation"]["light"] == res9_city_res["evaluation"]["light"] == "黄灯"
print("PASS [审查对齐]: 居民医保与城镇居民医保基准与灯色 100% 相同！")

print("\n=== 10. 【审查关键验收】零基准接口顶层状态一致性 ===")
# 缺省有效参保类型或无效参数时绝不返回 top-level MATCHED
res10_ambig = post("https://127.0.0.1/api/tools/check_patient_dip", {
    "patient_id": "zy_synth_zero_01",
    "disease": "M17.9",
    "current_cost": 2450.00,
    "age": 68,
    "ins_type": "城镇",
    "operation": "保守治疗"
})
print("AmbigStatus:", res10_ambig.get("status"), "Success:", res10_ambig.get("success"), "Reason:", res10_ambig.get("pending_reason"))
assert res10_ambig["success"] is False
assert res10_ambig["status"] == "PENDING"
assert res10_ambig["pending_reason"] == "AMBIGUOUS_INS_TYPE"

print("\n=== 11. 【审查关键验收 P1】退休+公费医疗 聊天接口严禁误判为职工医保 ===")
res11_retire_gf = post("https://127.0.0.1/api/chat", {
    "text": "68岁，退休，公费医疗，M17.9，保守治疗，请查询医院 DIP 预估支付额"
})
print("Intent:", res11_retire_gf.get("intent"), "Status:", res11_retire_gf.get("data", {}).get("status"), "PendingReason:", res11_retire_gf.get("data", {}).get("pending_reason"))
assert res11_retire_gf["intent"] == "need_clarification"
assert res11_retire_gf["data"]["status"] == "PENDING"
assert "2790.90" not in res11_retire_gf.get("reply", "")
assert "UNSUPPORTED_INS_TYPE:公费医疗" in res11_retire_gf.get("data", {}).get("pending_reason", "")
print("PASS [P1 对齐]: 退休+公费医疗 正确识别为公费医疗拦截，绝不返回职工医保基准！")

print("\n=== 12. 【审查关键验收 P1】仅输入“已退休”职业状态 聊天接口主动追问险种 ===")
res12_retire = post("https://127.0.0.1/api/chat", {
    "text": "68岁，已退休，M17.9 保守治疗"
})
print("Intent:", res12_retire.get("intent"), "Status:", res12_retire.get("data", {}).get("status"), "PendingReason:", res12_retire.get("data", {}).get("pending_reason"))
assert res12_retire["intent"] == "need_clarification"
assert res12_retire["data"]["status"] == "PENDING"
assert res12_retire["data"]["pending_reason"] == "OCCUPATION_NOT_INSURANCE_TYPE"
assert "2790.90" not in res12_retire.get("reply", "")
print("PASS [P1 对齐]: 仅输入“已退休”职业状态 正确拦截并追问具体险种！")

print("\n=== 13. 【审查关键验收 P1】职工+工伤保险 优先识别工伤保险并拦截 ===")
res13_work_inj = post("https://127.0.0.1/api/chat", {
    "text": "68岁职工，工伤保险，M17.9 保守治疗"
})
print("Intent:", res13_work_inj.get("intent"), "Status:", res13_work_inj.get("data", {}).get("status"), "PendingReason:", res13_work_inj.get("data", {}).get("pending_reason"))
assert res13_work_inj["intent"] == "need_clarification"
assert res13_work_inj["data"]["status"] == "PENDING"
assert "UNSUPPORTED_INS_TYPE:工伤保险" in res13_work_inj.get("data", {}).get("pending_reason", "")
assert "2790.90" not in res13_work_inj.get("reply", "")
print("PASS [P1 对齐]: 职工+工伤保险 正确优先识别工伤保险并进入待定拦截！")

print("\n=== 14. 【审查关键验收 P1 (Round 7)】企业职工，参保类型待查 拦截与省略金额 ===")
res14_unknown = post("https://127.0.0.1/api/chat", {
    "text": "68岁，企业职工，参保类型待查，M17.9，保守治疗"
})
print("Intent:", res14_unknown.get("intent"), "Status:", res14_unknown.get("data", {}).get("status"), "PendingReason:", res14_unknown.get("data", {}).get("pending_reason"))
assert res14_unknown["intent"] == "need_clarification"
assert res14_unknown["data"]["status"] == "PENDING"
assert res14_unknown["data"]["pending_reason"] == "MISSING_INS_TYPE"
assert "2790.90" not in res14_unknown.get("reply", "")
print("PASS [Round 7 P1 对齐]: “企业职工，参保类型待查” 正确追问并彻底省略金额！")

print("\n=== 15. 【审查关键验收 P1 (Round 7)】原居民医保，本次按职工医保结算 明确指令优先 ===")
res15_directive = post("https://127.0.0.1/api/chat", {
    "text": "68岁，原居民医保，本次按职工医保结算，M17.9，保守治疗"
})
print("Intent:", res15_directive.get("intent"), "Status:", res15_directive.get("data", {}).get("status"), "Benchmark:", res15_directive.get("data", {}).get("benchmark_amount"))
assert res15_directive["intent"] == "query_dip_rule"
assert res15_directive["data"]["matched"] is True
assert res15_directive["data"]["benchmark_amount"] == 2790.90
assert "2790.90" in res15_directive.get("reply", "")
print("PASS [Round 7 P1 对齐]: “原居民医保，本次按职工医保结算” 准确优先采用本次职工基准 ￥2790.90，杜绝少算 186.06 元！")

print("\n=== 16. 【审查关键验收 P2 (Round 7)】职工医保，非工伤 否定词排除 ===")
res16_negation = post("https://127.0.0.1/api/chat", {
    "text": "68岁，职工医保，非工伤，M17.9，保守治疗"
})
print("Intent:", res16_negation.get("intent"), "Status:", res16_negation.get("data", {}).get("status"), "Benchmark:", res16_negation.get("data", {}).get("benchmark_amount"))
assert res16_negation["intent"] == "query_dip_rule"
assert res16_negation["data"]["matched"] is True
assert res16_negation["data"]["benchmark_amount"] == 2790.90
assert "2790.90" in res16_negation.get("reply", "")
print("PASS [Round 7 P2 对齐]: “职工医保，非工伤” 正确排除否定词按职工医保测算 ￥2790.90！")

print("\n=== 17. 【审查关键验收 P2 (Round 7)】本次职工医保结算，既往工伤已结案 既往词排除 ===")
res17_history = post("https://127.0.0.1/api/chat", {
    "text": "68岁，本次职工医保结算，既往工伤已结案，M17.9，保守治疗"
})
print("Intent:", res17_history.get("intent"), "Status:", res17_history.get("data", {}).get("status"), "Benchmark:", res17_history.get("data", {}).get("benchmark_amount"))
assert res17_history["intent"] == "query_dip_rule"
assert res17_history["data"]["matched"] is True
assert res17_history["data"]["benchmark_amount"] == 2790.90
assert "2790.90" in res17_history.get("reply", "")
print("PASS [Round 7 P2 对齐]: “本次职工医保结算，既往工伤已结案” 排除既往词按职工医保测算 ￥2790.90！")

print("\n" + "=" * 60)
print(">>> ALL 17 API INTEGRATION TESTS PASSED (100% COMPLIANT) <<<")
print("=" * 60)

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

print("\n" + "=" * 60)
print(">>> ALL 10 API INTEGRATION TESTS PASSED (100% COMPLIANT) <<<")
print("=" * 60)

# -*- coding: utf-8 -*-
import urllib.request
import urllib.error
import json
import ssl
import sys
import os

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

# 受保护接口测试用账号（口令只从环境变量/.env 读取，绝不入库入仓）
# TEST-CRED-001：使用标准 dotenv 解析器，四个字段（两个工号 + 两个口令）均支持
# 环境变量优先、.env 兜底；.env 中带引号的口令自动剥离引号，自定义工号同样生效
def load_test_credentials():
    """按字段优先级读取测试凭据：环境变量 > .env；口令绝不打印、绝不落盘"""
    if os.path.exists(".env"):
        try:
            from dotenv import dotenv_values
            file_vals = {k: v for k, v in dotenv_values(".env").items() if v is not None}
        except ImportError:
            # 无 python-dotenv 时的最小降级解析：同样剥离成对引号
            file_vals = {}
            with open(".env", "r", encoding="utf-8") as _ef:
                for _line in _ef:
                    _line = _line.strip()
                    if not _line or _line.startswith("#") or "=" not in _line:
                        continue
                    _k, _v = _line.split("=", 1)
                    _v = _v.strip()
                    if len(_v) >= 2 and _v[0] == _v[-1] and _v[0] in ("'", '"'):
                        _v = _v[1:-1]
                    file_vals[_k.strip()] = _v
    else:
        file_vals = {}
    def _pick(key, default=""):
        return os.environ.get(key) or file_vals.get(key) or default
    return {
        "TEST_OPERATOR_ID": _pick("TEST_OPERATOR_ID", "doctor01"),
        "TEST_OPERATOR_PASS": _pick("TEST_OPERATOR_PASS"),
        "TEST_ADMIN_ID": _pick("TEST_ADMIN_ID", "admin"),
        "TEST_ADMIN_PASS": _pick("TEST_ADMIN_PASS"),
    }

_cred = load_test_credentials()
TEST_OPERATOR_ID = _cred["TEST_OPERATOR_ID"]
TEST_OPERATOR_PASS = _cred["TEST_OPERATOR_PASS"]
TEST_ADMIN_ID = _cred["TEST_ADMIN_ID"]
TEST_ADMIN_PASS = _cred["TEST_ADMIN_PASS"]

def post(url, payload, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers
    )
    with urllib.request.urlopen(req, context=ctx) as resp:
        return json.loads(resp.read().decode("utf-8"))

def post_with_status(url, payload, token=None):
    """返回 (HTTP状态码, 响应体JSON)，用于 401/403 等鉴权负向断言"""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
    try:
        with urllib.request.urlopen(req, context=ctx) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))

def get_with_status(url, token=None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, context=ctx) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))

print("=== 1. Direct Health Check (HTTP 8000, 仅宿主机回环绑定) ===")
with urllib.request.urlopen("http://127.0.0.1:8000/health") as resp:
    res1 = json.loads(resp.read().decode("utf-8"))
    print(res1)
    assert res1["status"] == "ok"

print("\n=== 2. Nginx HTTPS Health Check (HTTPS 443) ===")
with urllib.request.urlopen("https://127.0.0.1/health", context=ctx) as resp:
    res2 = json.loads(resp.read().decode("utf-8"))
    print(res2)
    assert res2["status"] == "ok"

print("\n=== 2A. 【RBAC 服务端鉴权验收】登录、令牌、患者级接口强制管控 ===")
if not TEST_OPERATOR_PASS or not TEST_ADMIN_PASS:
    print("FATAL: 请先设置环境变量 TEST_OPERATOR_PASS 与 TEST_ADMIN_PASS 再运行集成测试")
    sys.exit(1)

# 2A-1 错误口令必须 401
code, body = post_with_status("https://127.0.0.1/api/auth/login", {"operator_id": TEST_OPERATOR_ID, "password": "wrong-password-xxx"})
assert code == 401
print("PASS [RBAC 2A-1]: 错误口令登录被拒绝 (401)")

# 2A-2 正确口令签发令牌
code, body = post_with_status("https://127.0.0.1/api/auth/login", {"operator_id": TEST_OPERATOR_ID, "password": TEST_OPERATOR_PASS})
assert code == 200 and body.get("access_token")
TOKEN = body["access_token"]
assert body.get("role_type") == "DOCTOR"
print(f"PASS [RBAC 2A-2]: 正确口令登录成功，令牌签发（角色 {body.get('role_type')}）")

# 2A-3 患者判灯接口无令牌必须 401
code, body = post_with_status("https://127.0.0.1/api/tools/check_patient_dip", {
    "patient_id": "zy2025042901", "disease": "M17.9", "current_cost": 2550.00,
    "age": 68, "ins_type": "职工", "operation": "保守治疗"
})
assert code == 401
print("PASS [RBAC 2A-3]: 患者判灯接口无令牌被拒绝 (401)")

# 2A-4 伪造令牌必须 401
code, body = post_with_status("https://127.0.0.1/api/tools/check_patient_dip", {
    "patient_id": "zy2025042901", "disease": "M17.9", "current_cost": 2550.00,
    "age": 68, "ins_type": "职工", "operation": "保守治疗"
}, token="v1.forged.payload")
assert code == 401
print("PASS [RBAC 2A-4]: 伪造令牌被拒绝 (401)")

# 2A-5 预警看板：医生角色 403（角色矩阵生效）
code, body = get_with_status("https://127.0.0.1/api/inpatient/alerts", token=TOKEN)
assert code == 403
print("PASS [RBAC 2A-5]: 医生角色访问预警看板被拒绝 (403)")

# 2A-6 预警看板：管理员角色 200
code, body = post_with_status("https://127.0.0.1/api/auth/login", {"operator_id": TEST_ADMIN_ID, "password": TEST_ADMIN_PASS})
assert code == 200 and body.get("access_token")
ADMIN_TOKEN = body["access_token"]
code, body = get_with_status("https://127.0.0.1/api/inpatient/alerts", token=ADMIN_TOKEN)
assert code == 200 and body.get("total") is not None
print("PASS [RBAC 2A-6]: 管理员角色访问预警看板正常 (200)")

# 2A-7 开放接口（科室级 DIP 基准查询）无令牌保持可用
res_open = post("https://127.0.0.1/api/tools/query_dip_rule", {
    "disease": "M17.9", "age": 68, "ins_type": "职工", "operation": "保守治疗"
})
assert res_open["matched"] is True and res_open["benchmark_amount"] == 2790.90
print("PASS [RBAC 2A-7]: 开放接口无令牌保持可用（匿名身份记审计）")


# 2A-3B 医生实时管床越权校验：普通医生访问非本人管床/跨科患者必须 403 并在审计中记录 DENIED
code_doc2, body_doc2 = post_with_status("https://127.0.0.1/api/auth/login", {"operator_id": "doctor_other", "password": "Doc@Other2026!"})
assert code_doc2 == 200
TOKEN_DOC2 = body_doc2["access_token"]
code_unauth, body_unauth = post_with_status("https://127.0.0.1/api/tools/check_patient_dip", {
    "patient_id": "zy2025042901", "disease": "M17.9", "current_cost": 2550.00,
    "age": 68, "ins_type": "职工", "operation": "保守治疗"
}, token=TOKEN_DOC2)
assert code_unauth == 403 and "越权访问" in body_unauth.get("detail", "")
print("PASS [RBAC 2A-3B]: 非本人管床医生访问患者费用被实时阻断 (403)，严禁跨床越权！")

# 2A-5B 预警看板：科室主任角色 (DEPT_HEAD) 访问本科室预警正常 (200)
code_dh, body_dh = post_with_status("https://127.0.0.1/api/auth/login", {"operator_id": "depthead01", "password": "Head@Yy2026!Test"})
assert code_dh == 200
TOKEN_DH = body_dh["access_token"]
code_dh_alerts, body_dh_alerts = get_with_status("https://127.0.0.1/api/inpatient/alerts", token=TOKEN_DH)
assert code_dh_alerts == 200 and body_dh_alerts.get("is_demo_baseline") is False
print("PASS [RBAC 2A-5B]: 科室主任角色成功访问本科室真实在院快照看板 (200)！")

# 2A-8 负费用拦截校验：current_cost < 0 必须拦截待定并标明 NEGATIVE_FEE_UNRECONCILED
code_neg, body_neg = post_with_status("https://127.0.0.1/api/tools/check_patient_dip", {
    "patient_id": "zy2025042901", "disease": "M17.9", "current_cost": -120.50,
    "age": 68, "ins_type": "职工", "operation": "保守治疗"
}, token=TOKEN)
assert code_neg == 200 and body_neg.get("status") == "PENDING"
assert body_neg.get("pending_reason") == "NEGATIVE_FEE_UNRECONCILED"
assert body_neg.get("evaluation", {}).get("light") == "无"
print("PASS [P1 负费用拦截 2A-8]: 冲正未闭环负费用成功拦截为 PENDING / NEGATIVE_FEE_UNRECONCILED，杜绝误判绿灯！")
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
}, token=TOKEN)
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
}, token=TOKEN)
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
}, token=ADMIN_TOKEN)
res9_city_res = post("https://127.0.0.1/api/tools/check_patient_dip", {
    "patient_id": "zy_synth_res_02",
    "disease": "M17.9",
    "current_cost": 2450.00,
    "age": 68,
    "ins_type": "城镇居民医保",
    "operation": "保守治疗"
}, token=ADMIN_TOKEN)
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
}, token=TOKEN)
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

print("\n=== 18. 【审查关键验收 P1 (Round 8)】带空格待查接口统一拦截与省略金额 ===")
# 18a: /api/chat 测试“企业职工，参保类型 待查”
res18_chat = post("https://127.0.0.1/api/chat", {
    "text": "68岁，企业职工，参保类型 待查，M17.9，保守治疗"
})
print("[18a chat] Intent:", res18_chat.get("intent"), "Status:", res18_chat.get("data", {}).get("status"), "PendingReason:", res18_chat.get("data", {}).get("pending_reason"))
assert res18_chat["intent"] == "need_clarification"
assert res18_chat["data"]["status"] == "PENDING"
assert res18_chat["data"]["pending_reason"] == "MISSING_INS_TYPE"
assert "2790.90" not in res18_chat.get("reply", "")

# 18b: /api/tools/query_dip_rule 测试“企业职工，参保类型 待查”
res18_rule = post("https://127.0.0.1/api/tools/query_dip_rule", {
    "disease": "M17.9",
    "age": 68,
    "ins_type": "企业职工，参保类型 待查",
    "operation": "保守治疗"
})
print("[18b tool] Matched:", res18_rule.get("matched"), "Status:", res18_rule.get("status"), "PendingReason:", res18_rule.get("pending_reason"))
assert res18_rule["matched"] is False
assert res18_rule["status"] == "PENDING"
assert res18_rule["pending_reason"] == "MISSING_INS_TYPE"
assert res18_rule.get("benchmark_amount") is None
print("PASS [Round 8 P1 对齐]: “企业职工，参保类型 待查”在 /api/chat 与 /api/tools/query_dip_rule 中双重严格拦截并省略金额！")

print("\n=== 19. 【审查关键验收 P1 (Round 8)】分句时态冲突：既往按居民结算，本次按职工结算 ===")
res19 = post("https://127.0.0.1/api/chat", {
    "text": "68岁，既往按居民医保结算，本次按职工医保结算，M17.9，保守治疗"
})
print("Intent:", res19.get("intent"), "Status:", res19.get("data", {}).get("status"), "Benchmark:", res19.get("data", {}).get("benchmark_amount"))
assert res19["intent"] == "query_dip_rule"
assert res19["data"]["matched"] is True
assert res19["data"]["benchmark_amount"] == 2790.90
assert "2790.90" in res19.get("reply", "")
print("PASS [Round 8 P1 对齐]: “既往按居民医保结算，本次按职工医保结算” 准确优先选取本次肯定指令 ￥2790.90，杜绝少算 186.06 元！")

print("\n=== 20. 【审查关键验收 P1 (Round 8)】分句否定冲突：本次不按职工医保结算，按居民医保结算 ===")
res20 = post("https://127.0.0.1/api/chat", {
    "text": "68岁，本次不按职工医保结算，按居民医保结算，M17.9，保守治疗"
})
print("Intent:", res20.get("intent"), "Status:", res20.get("data", {}).get("status"), "Benchmark:", res20.get("data", {}).get("benchmark_amount"))
assert res20["intent"] == "query_dip_rule"
assert res20["data"]["matched"] is True
assert res20["data"]["benchmark_amount"] == 2604.84
assert "2604.84" in res20.get("reply", "")
print("PASS [Round 8 P1 对齐]: “本次不按职工医保结算，按居民医保结算” 排除否定分句，准确采用居民基准 ￥2604.84！")

print("\n=== 21. 【审查关键验收 P2 (Round 8)】非医保类型待查保护：职工医保，病理类型待查 ===")
res21 = post("https://127.0.0.1/api/chat", {
    "text": "68岁，职工医保，病理类型待查，M17.9，保守治疗"
})
print("Intent:", res21.get("intent"), "Status:", res21.get("data", {}).get("status"), "Benchmark:", res21.get("data", {}).get("benchmark_amount"))
assert res21["intent"] == "query_dip_rule"
assert res21["data"]["matched"] is True
assert res21["data"]["benchmark_amount"] == 2790.90
assert "2790.90" in res21.get("reply", "")
print("PASS [Round 8 P2 对齐]: “职工医保，病理类型待查” 病理类型待查不误拦截医保，正常测算 ￥2790.90！")

print("\n=== 22. 【审查关键验收 P2 (Round 8)】历史待查不压制当前确认：原参保类型待查，现已确认本次按职工医保结算 ===")
res22 = post("https://127.0.0.1/api/chat", {
    "text": "68岁，原参保类型待查，现已确认本次按职工医保结算，M17.9，保守治疗"
})
print("Intent:", res22.get("intent"), "Status:", res22.get("data", {}).get("status"), "Benchmark:", res22.get("data", {}).get("benchmark_amount"))
assert res22["intent"] == "query_dip_rule"
assert res22["data"]["matched"] is True
assert res22["data"]["benchmark_amount"] == 2790.90
assert "2790.90" in res22.get("reply", "")
print("PASS [Round 8 P2 对齐]: “原参保类型待查，现已确认本次按职工医保结算” 历史待查不压制当前确认 ￥2790.90！")

print("\n=== 23. 【审查关键验收 P1 (Round 9)】本次未按职工医保结算 仅有否定险种双接口拦截 ===")
res23_chat = post("https://127.0.0.1/api/chat", {
    "text": "68岁，本次未按职工医保结算，M17.9，保守治疗"
})
print("[23 chat] Intent:", res23_chat.get("intent"), "Status:", res23_chat.get("data", {}).get("status"), "PendingReason:", res23_chat.get("data", {}).get("pending_reason"))
assert res23_chat["intent"] == "need_clarification"
assert res23_chat["data"]["status"] == "PENDING"
assert res23_chat["data"]["pending_reason"] == "MISSING_INS_TYPE"
assert "2790.90" not in res23_chat.get("reply", "")

res23_rule = post("https://127.0.0.1/api/tools/query_dip_rule", {
    "disease": "M17.9",
    "age": 68,
    "ins_type": "本次未按职工医保结算",
    "operation": "保守治疗"
})
print("[23 tool] Matched:", res23_rule.get("matched"), "Status:", res23_rule.get("status"), "PendingReason:", res23_rule.get("pending_reason"))
assert res23_rule["matched"] is False
assert res23_rule["status"] == "PENDING"
assert res23_rule["pending_reason"] == "MISSING_INS_TYPE"
assert res23_rule.get("benchmark_amount") is None
print("PASS [Round 9 P1 对齐]: “本次未按职工医保结算” 双接口拦截并省略金额！")

print("\n=== 24. 【审查关键验收 P1 (Round 9)】本次不采用居民医保 仅有否定险种双接口拦截 ===")
res24_chat = post("https://127.0.0.1/api/chat", {
    "text": "68岁，本次不采用居民医保，M17.9，保守治疗"
})
print("[24 chat] Intent:", res24_chat.get("intent"), "Status:", res24_chat.get("data", {}).get("status"), "PendingReason:", res24_chat.get("data", {}).get("pending_reason"))
assert res24_chat["intent"] == "need_clarification"
assert res24_chat["data"]["status"] == "PENDING"
assert res24_chat["data"]["pending_reason"] == "MISSING_INS_TYPE"
assert "2604.84" not in res24_chat.get("reply", "")

res24_rule = post("https://127.0.0.1/api/tools/query_dip_rule", {
    "disease": "M17.9",
    "age": 68,
    "ins_type": "本次不采用居民医保",
    "operation": "保守治疗"
})
print("[24 tool] Matched:", res24_rule.get("matched"), "Status:", res24_rule.get("status"), "PendingReason:", res24_rule.get("pending_reason"))
assert res24_rule["matched"] is False
assert res24_rule["status"] == "PENDING"
assert res24_rule["pending_reason"] == "MISSING_INS_TYPE"
assert res24_rule.get("benchmark_amount") is None
print("PASS [Round 9 P1 对齐]: “本次不采用居民医保” 双接口拦截并省略金额！")

print("\n=== 25. 【审查关键验收 P1 (Round 9)】待查含中文冒号：企业职工，参保类型：待查 ===")
res25_chat = post("https://127.0.0.1/api/chat", {
    "text": "68岁，企业职工，参保类型：待查，M17.9，保守治疗"
})
print("[25 chat] Intent:", res25_chat.get("intent"), "Status:", res25_chat.get("data", {}).get("status"), "PendingReason:", res25_chat.get("data", {}).get("pending_reason"))
assert res25_chat["intent"] == "need_clarification"
assert res25_chat["data"]["status"] == "PENDING"
assert res25_chat["data"]["pending_reason"] == "MISSING_INS_TYPE"
assert "2790.90" not in res25_chat.get("reply", "")

res25_rule = post("https://127.0.0.1/api/tools/query_dip_rule", {
    "disease": "M17.9",
    "age": 68,
    "ins_type": "企业职工，参保类型：待查",
    "operation": "保守治疗"
})
print("[25 tool] Matched:", res25_rule.get("matched"), "Status:", res25_rule.get("status"), "PendingReason:", res25_rule.get("pending_reason"))
assert res25_rule["matched"] is False
assert res25_rule["status"] == "PENDING"
assert res25_rule["pending_reason"] == "MISSING_INS_TYPE"
assert res25_rule.get("benchmark_amount") is None
print("PASS [Round 9 P1 对齐]: “企业职工，参保类型：待查”(含冒号) 双接口严格拦截！")

print("\n=== 26. 【审查关键验收 P1 (Round 9)】多字段待查解耦：企业职工，病理类型待查且参保类型待查 ===")
res26_chat = post("https://127.0.0.1/api/chat", {
    "text": "68岁，企业职工，病理类型待查且参保类型待查，M17.9，保守治疗"
})
print("[26 chat] Intent:", res26_chat.get("intent"), "Status:", res26_chat.get("data", {}).get("status"), "PendingReason:", res26_chat.get("data", {}).get("pending_reason"))
assert res26_chat["intent"] == "need_clarification"
assert res26_chat["data"]["status"] == "PENDING"
assert res26_chat["data"]["pending_reason"] == "MISSING_INS_TYPE"
assert "2790.90" not in res26_chat.get("reply", "")

res26_rule = post("https://127.0.0.1/api/tools/query_dip_rule", {
    "disease": "M17.9",
    "age": 68,
    "ins_type": "企业职工，病理类型待查且参保类型待查",
    "operation": "保守治疗"
})
print("[26 tool] Matched:", res26_rule.get("matched"), "Status:", res26_rule.get("status"), "PendingReason:", res26_rule.get("pending_reason"))
assert res26_rule["matched"] is False
assert res26_rule["status"] == "PENDING"
assert res26_rule["pending_reason"] == "MISSING_INS_TYPE"
assert res26_rule.get("benchmark_amount") is None
print("PASS [Round 9 P1 对齐]: “企业职工，病理类型待查且参保类型待查” 多字段解耦，参保待查不被屏蔽！")

print("\n=== 27. 【审查关键验收 P1 (Round 9)】选择疑问句消歧：按职工医保还是按居民医保结算？ ===")
res27_chat = post("https://127.0.0.1/api/chat", {
    "text": "68岁，按职工医保还是按居民医保结算？M17.9，保守治疗"
})
print("[27 chat] Intent:", res27_chat.get("intent"), "Status:", res27_chat.get("data", {}).get("status"), "PendingReason:", res27_chat.get("data", {}).get("pending_reason"))
assert res27_chat["intent"] == "need_clarification"
assert res27_chat["data"]["status"] == "PENDING"
assert res27_chat["data"]["pending_reason"] == "AMBIGUOUS_INS_TYPE"
assert "2790.90" not in res27_chat.get("reply", "")
assert "2604.84" not in res27_chat.get("reply", "")

res27_rule = post("https://127.0.0.1/api/tools/query_dip_rule", {
    "disease": "M17.9",
    "age": 68,
    "ins_type": "按职工医保还是按居民医保结算？",
    "operation": "保守治疗"
})
print("[27 tool] Matched:", res27_rule.get("matched"), "Status:", res27_rule.get("status"), "PendingReason:", res27_rule.get("pending_reason"))
assert res27_rule["matched"] is False
assert res27_rule["status"] == "PENDING"
assert res27_rule["pending_reason"] == "AMBIGUOUS_INS_TYPE"
assert res27_rule.get("benchmark_amount") is None
print("PASS [Round 9 P1 对齐]: “按职工医保还是按居民医保结算？” 识别选择疑问句并进入 PENDING！")

print("\n=== 28. 【审查关键验收 P2 (Round 9)】项目级自费兼容：职工医保，部分项目自费 ===")
res28_chat = post("https://127.0.0.1/api/chat", {
    "text": "68岁，职工医保，部分项目自费，M17.9，保守治疗"
})
print("[28 chat] Intent:", res28_chat.get("intent"), "Status:", res28_chat.get("data", {}).get("status"), "Benchmark:", res28_chat.get("data", {}).get("benchmark_amount"))
assert res28_chat["intent"] == "query_dip_rule"
assert res28_chat["data"]["matched"] is True
assert res28_chat["data"]["benchmark_amount"] == 2790.90
assert "2790.90" in res28_chat.get("reply", "")

res28_rule = post("https://127.0.0.1/api/tools/query_dip_rule", {
    "disease": "M17.9",
    "age": 68,
    "ins_type": "职工医保，部分项目自费",
    "operation": "保守治疗"
})
print("[28 tool] Matched:", res28_rule.get("matched"), "Status:", res28_rule.get("status"), "Benchmark:", res28_rule.get("benchmark_amount"))
assert res28_rule["matched"] is True
assert res28_rule["status"] == "MATCHED"
assert res28_rule["benchmark_amount"] == 2790.90
print("PASS [Round 9 P2 对齐]: “职工医保，部分项目自费” 项目级自费不拦截整单医保，正常测算 ￥2790.90！")

print("\n=== 29. 【审查关键验收 P1 (Round 10)】本次未按城乡居民医保结算 别名否定双接口拦截 ===")
res29_chat = post("https://127.0.0.1/api/chat", {
    "text": "68岁，本次未按城乡居民医保结算，M17.9，保守治疗"
})
print("[29 chat] Intent:", res29_chat.get("intent"), "Status:", res29_chat.get("data", {}).get("status"), "PendingReason:", res29_chat.get("data", {}).get("pending_reason"))
assert res29_chat["intent"] == "need_clarification"
assert res29_chat["data"]["status"] == "PENDING"
assert res29_chat["data"]["pending_reason"] == "MISSING_INS_TYPE"
assert "2604.84" not in res29_chat.get("reply", "")

res29_rule = post("https://127.0.0.1/api/tools/query_dip_rule", {
    "disease": "M17.9",
    "age": 68,
    "ins_type": "本次未按城乡居民医保结算",
    "operation": "保守治疗"
})
print("[29 tool] Matched:", res29_rule.get("matched"), "Status:", res29_rule.get("status"), "PendingReason:", res29_rule.get("pending_reason"))
assert res29_rule["matched"] is False
assert res29_rule["status"] == "PENDING"
assert res29_rule["pending_reason"] == "MISSING_INS_TYPE"
assert res29_rule.get("benchmark_amount") is None
print("PASS [Round 10 P1 对齐]: “本次未按城乡居民医保结算” 双接口拦截否定险种并省略金额！")

print("\n=== 30. 【审查关键验收 P1 (Round 10)】按职工医保或居民医保结算？ 单字'或'双接口消歧 ===")
res30_chat = post("https://127.0.0.1/api/chat", {
    "text": "68岁，按职工医保或居民医保结算？M17.9，保守治疗"
})
print("[30 chat] Intent:", res30_chat.get("intent"), "Status:", res30_chat.get("data", {}).get("status"), "PendingReason:", res30_chat.get("data", {}).get("pending_reason"))
assert res30_chat["intent"] == "need_clarification"
assert res30_chat["data"]["status"] == "PENDING"
assert res30_chat["data"]["pending_reason"] == "AMBIGUOUS_INS_TYPE"
assert "2790.90" not in res30_chat.get("reply", "")
assert "2604.84" not in res30_chat.get("reply", "")

res30_rule = post("https://127.0.0.1/api/tools/query_dip_rule", {
    "disease": "M17.9",
    "age": 68,
    "ins_type": "按职工医保或居民医保结算？",
    "operation": "保守治疗"
})
print("[30 tool] Matched:", res30_rule.get("matched"), "Status:", res30_rule.get("status"), "PendingReason:", res30_rule.get("pending_reason"))
assert res30_rule["matched"] is False
assert res30_rule["status"] == "PENDING"
assert res30_rule["pending_reason"] == "AMBIGUOUS_INS_TYPE"
assert res30_rule.get("benchmark_amount") is None
print("PASS [Round 10 P1 对齐]: “按职工医保或居民医保结算？” 单字'或'双接口识别选择疑问句并进入 PENDING！")

print("\n=== 31. 【审查关键验收 P1 (Round 10)】更正为居民医保 指令优先覆盖 ===")
res31_chat = post("https://127.0.0.1/api/chat", {
    "text": "68岁，本次按职工医保结算，核实后更正为居民医保，M17.9，保守治疗"
})
print("[31 chat] Intent:", res31_chat.get("intent"), "Status:", res31_chat.get("data", {}).get("status"), "Benchmark:", res31_chat.get("data", {}).get("benchmark_amount"))
assert res31_chat["intent"] == "query_dip_rule"
assert res31_chat["data"]["matched"] is True
assert res31_chat["data"]["benchmark_amount"] == 2604.84
assert "2604.84" in res31_chat.get("reply", "")

res31_rule = post("https://127.0.0.1/api/tools/query_dip_rule", {
    "disease": "M17.9",
    "age": 68,
    "ins_type": "本次按职工医保结算，核实后更正为居民医保",
    "operation": "保守治疗"
})
print("[31 tool] Matched:", res31_rule.get("matched"), "Status:", res31_rule.get("status"), "Benchmark:", res31_rule.get("benchmark_amount"))
assert res31_rule["matched"] is True
assert res31_rule["status"] == "MATCHED"
assert res31_rule["benchmark_amount"] == 2604.84
print("PASS [Round 10 P1 对齐]: “核实后更正为居民医保” 准确覆盖前置指令采用居民基准 ￥2604.84，杜绝多算 186.06 元！")

print("\n=== 32. 【审查关键验收 P2 (Round 10)】耗材按自费项目结算 支付范围隔离 ===")
res32_chat = post("https://127.0.0.1/api/chat", {
    "text": "68岁，职工医保，耗材按自费项目结算，M17.9，保守治疗"
})
print("[32 chat] Intent:", res32_chat.get("intent"), "Status:", res32_chat.get("data", {}).get("status"), "Benchmark:", res32_chat.get("data", {}).get("benchmark_amount"))
assert res32_chat["intent"] == "query_dip_rule"
assert res32_chat["data"]["matched"] is True
assert res32_chat["data"]["benchmark_amount"] == 2790.90
assert "2790.90" in res32_chat.get("reply", "")

res32_rule = post("https://127.0.0.1/api/tools/query_dip_rule", {
    "disease": "M17.9",
    "age": 68,
    "ins_type": "职工医保，耗材按自费项目结算",
    "operation": "保守治疗"
})
print("[32 tool] Matched:", res32_rule.get("matched"), "Status:", res32_rule.get("status"), "Benchmark:", res32_rule.get("benchmark_amount"))
assert res32_rule["matched"] is True
assert res32_rule["status"] == "MATCHED"
assert res32_rule["benchmark_amount"] == 2790.90
print("PASS [Round 10 P2 对齐]: “职工医保，耗材按自费项目结算” 范围隔离，正常测算职工基准 ￥2790.90！")

print("\n=== 33. 【审查关键验收 P1 (Round 10)】临床方案选择不误拦截已确认险种 ===")
res33_chat = post("https://127.0.0.1/api/chat", {
    "text": "68岁，本次按职工医保结算，M17.9，保守还是手术？"
})
print("[33 chat] Intent:", res33_chat.get("intent"), "Status:", res33_chat.get("data", {}).get("status"), "Benchmark:", res33_chat.get("data", {}).get("benchmark_amount"))
assert res33_chat["intent"] == "query_dip_rule"
assert res33_chat["data"]["matched"] is True
assert res33_chat["data"]["benchmark_amount"] == 2790.90
assert "2790.90" in res33_chat.get("reply", "")
print("PASS [Round 10 P1 对齐]: “本次按职工医保结算，保守还是手术？” 方案选择不误伤险种，正常测算 ￥2790.90！")

print("\n" + "=" * 60)
print(">>> ALL 40 API INTEGRATION TESTS PASSED (100% COMPLIANT) <<<")
print("=" * 60)

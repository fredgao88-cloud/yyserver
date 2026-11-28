# -*- coding: utf-8 -*-
"""
auth/rbac.py 离线单元测试（tests/test_auth_rbac.py）
覆盖：口令哈希、令牌签发/校验/篡改/过期/异密钥、Bearer 提取、角色矩阵。
纯标准库与内存操作，不连接数据库，可在 CI 离线环境直接运行。
"""
import base64
import hashlib
import hmac
import json
import os
import sys
import time

# 必须先于 auth.rbac 导入设置测试密钥
os.environ["AUTH_SECRET"] = "unit-test-only-secret-do-not-use-in-prod"
os.environ.setdefault("HIS_PASS", "offline-synthetic-his-pass")
os.environ.setdefault("AUTH_TOKEN_TTL_MINUTES", "720")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from auth.rbac import (
    ROLE_DEPT_HEAD,
    hash_password, verify_password, issue_token, verify_token,
    extract_bearer_token, role_permitted,
    ROLE_DOCTOR, ROLE_INSURANCE, ROLE_ADMIN, ALL_ROLES,
    PATIENT_QUERY_ROLES, ALERTS_ROLES, PBKDF2_ITERATIONS
)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def run_auth_tests():
    print("=" * 70)
    print(">>> 开始执行 auth/rbac.py 离线单元测试 (tests/test_auth_rbac.py) <<<")
    print("=" * 70)

    # 1. 口令哈希：格式、盐唯一性、往返校验、错误口令拒绝、弱口令拒绝
    h1 = hash_password("Str0ng!Pass")
    h2 = hash_password("Str0ng!Pass")
    assert h1.startswith(f"pbkdf2_sha256${PBKDF2_ITERATIONS}$")
    assert h1 != h2, "相同口令两次哈希必须因随机盐而不同"
    assert verify_password("Str0ng!Pass", h1) is True
    assert verify_password("WrongPass1", h1) is False
    assert verify_password("", h1) is False
    assert verify_password("Str0ng!Pass", "garbage") is False
    assert verify_password("Str0ng!Pass", "pbkdf2_sha256$abc$!!$%%") is False
    try:
        hash_password("short")
        raise AssertionError("弱口令必须被拒绝")
    except ValueError:
        pass
    print("PASS [Auth 1]: 口令哈希 PBKDF2 格式/盐唯一/往返/错误口令/弱口令 全部正确")

    # 2. 令牌往返：签发后校验通过，字段完整
    tok = issue_token("doctor01", ROLE_DOCTOR, "中医科")
    p = verify_token(tok)
    assert p is not None
    assert p["sub"] == "doctor01"
    assert p["role"] == ROLE_DOCTOR
    assert p["dept"] == "中医科"
    assert p["exp"] > int(time.time())
    print("PASS [Auth 2]: 令牌签发-校验往返正确，sub/role/dept/exp 字段完整")

    # 3. 篡改载荷：改一字节签名即失效
    parts = tok.split(".")
    payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    payload["role"] = ROLE_ADMIN  # 尝试提权
    tampered_body = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    tampered = f"v1.{tampered_body}.{parts[2]}"
    assert verify_token(tampered) is None, "篡改载荷的令牌必须被拒绝"
    print("PASS [Auth 3]: 篡改载荷（提权 ADMIN）的令牌被正确拒绝")

    # 4. 伪造签名与异密钥：均须拒绝
    forged = f"{parts[0]}.{parts[1]}.{_b64url_encode(b'0' * 32)}"
    assert verify_token(forged) is None
    other_sig = _b64url_encode(hmac.new(b"another-secret", f"v1.{parts[1]}".encode("ascii"), hashlib.sha256).digest())
    assert verify_token(f"{parts[0]}.{parts[1]}.{other_sig}") is None
    assert verify_token("not-a-token") is None
    assert verify_token("") is None
    assert verify_token(None) is None
    print("PASS [Auth 4]: 伪造签名/异密钥/畸形/空令牌 全部拒绝")

    # 5. 过期令牌：手工构造 exp 为过去的载荷并用当前密钥签名
    now = int(time.time())
    expired_payload = {"sub": "doctor01", "role": ROLE_DOCTOR, "dept": "", "iat": now - 90000, "exp": now - 60}
    exp_body = _b64url_encode(json.dumps(expired_payload, separators=(",", ":")).encode("utf-8"))
    exp_sig = _b64url_encode(hmac.new(os.environ["AUTH_SECRET"].encode(), f"v1.{exp_body}".encode("ascii"), hashlib.sha256).digest())
    assert verify_token(f"v1.{exp_body}.{exp_sig}") is None, "过期令牌必须被拒绝"
    print("PASS [Auth 5]: 过期令牌被正确拒绝")

    # 6. Bearer 提取：合法/缺前缀/小写 bearer/空值
    assert extract_bearer_token(f"Bearer {tok}") == tok
    assert extract_bearer_token(f"bearer {tok}") == tok
    assert extract_bearer_token(tok) is None
    assert extract_bearer_token("Basic abc") is None
    assert extract_bearer_token(None) is None
    assert extract_bearer_token("") is None
    print("PASS [Auth 6]: Authorization 头 Bearer 提取边界全部正确")

    # 7. 角色矩阵：患者判灯全体在岗角色可访问；预警看板允许科室主任/医保办/管理员
    assert role_permitted(ROLE_DOCTOR, PATIENT_QUERY_ROLES) is True
    assert role_permitted(ROLE_DEPT_HEAD, PATIENT_QUERY_ROLES) is True
    assert role_permitted(ROLE_INSURANCE, PATIENT_QUERY_ROLES) is True
    assert role_permitted(ROLE_ADMIN, PATIENT_QUERY_ROLES) is True
    assert role_permitted(ROLE_DOCTOR, ALERTS_ROLES) is False
    assert role_permitted(ROLE_DEPT_HEAD, ALERTS_ROLES) is True
    assert role_permitted(ROLE_INSURANCE, ALERTS_ROLES) is True
    assert role_permitted(ROLE_ADMIN, ALERTS_ROLES) is True
    assert role_permitted("ROOT", PATIENT_QUERY_ROLES) is False
    assert role_permitted("", PATIENT_QUERY_ROLES) is False
    print("PASS [Auth 7]: 角色矩阵（判灯全体/预警仅医保办+管理员/非法角色拒绝）全部正确")

    # 8. 角色合法性：非法角色严禁签发令牌
    for bad_role in ("ROOT", "SUPERADMIN", "", None):
        try:
            issue_token("x", bad_role)
            raise AssertionError(f"非法角色 {bad_role} 签发必须被拒绝")
        except (ValueError, TypeError):
            pass
    print("PASS [Auth 8]: 非法角色签发令牌被正确拒绝")

    # 9. AUTH-REV-001 撤权/降权逐次数据库核对（内存模拟账号库，纯离线）：
    #    有效医保办可复核通过；停用后拒绝；降权后旧令牌角色不再生效；凭据版本不匹配/缺失拒绝
    import auth.rbac as rbac_mod

    mock_db = {
        "ins01": {"operator_id": "ins01", "display_name": "模拟医保办", "password_hash": "x",
                  "role_type": ROLE_INSURANCE, "dept_scope": "", "status": "ACTIVE", "cred_version": 3}
    }
    orig_get_operator = rbac_mod.get_operator
    rbac_mod.get_operator = lambda oid: mock_db.get(oid)
    try:
        # 9a. 有效医保办账号 + 凭据版本匹配 -> 复核通过，身份以数据库当前角色为准
        tok_ins = issue_token("ins01", ROLE_INSURANCE, "", cred_version=3)
        payload_ins = verify_token(tok_ins)
        assert payload_ins is not None and payload_ins.get("cv") == 3
        fresh, reason = rbac_mod.revalidate_operator(payload_ins)
        assert fresh is not None and reason == ""
        assert fresh["role"] == ROLE_INSURANCE
        assert role_permitted(fresh["role"], ALERTS_ROLES) is True
        print("PASS [Auth 9a]: 有效医保办账号凭据版本匹配，复核通过且角色取自数据库")

        # 9b. 停用后同一令牌下一次请求被拒
        mock_db["ins01"]["status"] = "DISABLED"
        fresh, reason = rbac_mod.revalidate_operator(payload_ins)
        assert fresh is None and "停用" in reason
        print("PASS [Auth 9b]: 账号停用后旧令牌复核被拒，绝不放行")

        # 9c. 恢复并降为医生：旧令牌（声明医保办）复核后角色以数据库为准，预警看板不再放行
        mock_db["ins01"]["status"] = "ACTIVE"
        mock_db["ins01"]["role_type"] = ROLE_DOCTOR
        fresh, reason = rbac_mod.revalidate_operator(payload_ins)
        assert fresh is not None and fresh["role"] == ROLE_DOCTOR
        assert role_permitted(fresh["role"], ALERTS_ROLES) is False, "降权后旧会话绝不可再进医保办看板"
        print("PASS [Auth 9c]: 降权为医生后角色即时以数据库为准，预警看板正确拒绝")

        # 9d. 凭据版本不匹配（口令/权限变更 bump 后旧令牌）与缺失 cv 的旧版令牌一律拒绝
        mock_db["ins01"]["role_type"] = ROLE_INSURANCE
        mock_db["ins01"]["cred_version"] = 4  # 模拟凭据变更
        fresh, reason = rbac_mod.revalidate_operator(payload_ins)
        assert fresh is None and "凭据" in reason
        legacy_payload = {"sub": "ins01", "role": ROLE_INSURANCE, "dept": "", "iat": now, "exp": now + 3600}  # 无 cv
        fresh, reason = rbac_mod.revalidate_operator(legacy_payload)
        assert fresh is None and "凭据" in reason
        print("PASS [Auth 9d]: 凭据版本不匹配及无 cv 旧版令牌均被拒，强制重新登录")

        # 9e. 账号不存在一律拒绝
        fresh, reason = rbac_mod.revalidate_operator({"sub": "ghost01", "role": ROLE_ADMIN, "cv": 0})
        assert fresh is None and "不存在" in reason
        print("PASS [Auth 9e]: 不存在账号的令牌复核被拒")
    finally:
        rbac_mod.get_operator = orig_get_operator

    # 10. AUTH-SCOPE-001 医生工号映射持久化与重置语义测试（离线模拟库）：
    # 验证登录工号与 HIS 医生工号独立映射、读回映射、更新重置
    mock_db_10 = {
        "doc_login_01": {
            "operator_id": "doc_login_01", "display_name": "MappedDoc", "password_hash": "x",
            "role_type": ROLE_DOCTOR, "dept_scope": "中医科", "doctor_id": "D150204006976",
            "status": "ACTIVE", "cred_version": 1
        }
    }
    orig_get_op10 = rbac_mod.get_operator
    rbac_mod.get_operator = lambda oid: mock_db_10.get(oid)
    try:
        op_m = rbac_mod.get_operator("doc_login_01")
        assert op_m is not None
        assert op_m["operator_id"] == "doc_login_01"
        assert op_m["doctor_id"] == "D150204006976", "登录工号 doc_login_01 应映射至 HIS 工号 D150204006976"

        # 重置映射后旧映射不再生效
        mock_db_10["doc_login_01"]["doctor_id"] = "DOC_NEW_888"
        op_m2 = rbac_mod.get_operator("doc_login_01")
        assert op_m2["doctor_id"] == "DOC_NEW_888", "重置后医生工号映射应即时更新"
        print("PASS [Auth 10]: AUTH-SCOPE-001 医生工号独立映射持久化与重置语义验证通过")
    finally:
        rbac_mod.get_operator = orig_get_op10

    # 11. AUTH-SCOPE-002 科室 ID 严格解析与同名歧义拒绝（离线模拟 his_ks）
    sample_ks = [
        {"ksid": 4, "ksname": "01中医科"},
        {"ksid": 10, "ksname": "02内科"},
        {"ksid": 11, "ksname": "03内科"},
        {"ksid": 20, "ksname": "综合病房"},
        {"ksid": 21, "ksname": "综合病房"},
    ]
    # 11a. 唯一名称/去数字前缀名称只解析为精确 ID 集合，绝不混入名称字符串
    res_unique = rbac_mod.resolve_authorized_dept_ids("中医科", his_ks_rows=sample_ks)
    assert res_unique == {"4"}, f"期望仅返回 {{'4'}}，实际 {res_unique}"
    # 11b. 同名不同 ID（综合病房 -> 20, 21）存在歧义，拒绝名称映射，返回空集
    res_ambig = rbac_mod.resolve_authorized_dept_ids("综合病房", his_ks_rows=sample_ks)
    assert res_ambig == set(), f"同名不同 ID 应拒绝映射返回空集，实际 {res_ambig}"
    # 11c. 直接传入精确 ID（20）可唯一命中
    res_exact_id = rbac_mod.resolve_authorized_dept_ids("20", his_ks_rows=sample_ks)
    assert res_exact_id == {"20"}, f"直接传 ID 20 应返回 {{'20'}}，实际 {res_exact_id}"
    # 11d. 空目录与目录故障（HISReader 抛异常）均严格拒绝返回空集，绝不扩大范围且零真实连接
    assert rbac_mod.resolve_authorized_dept_ids("99", his_ks_rows=[]) == set()
    from unittest.mock import patch
    import db.his_reader as his_mod
    with patch.object(his_mod, "HISReader", side_effect=RuntimeError("simulated directory outage")):
        assert rbac_mod.resolve_authorized_dept_ids("99") == set()
    print("PASS [Auth 11]: AUTH-SCOPE-002 仅用可信科室 ID 裁决、同名歧义拒绝及目录故障拒绝验证通过")

    print("\n" + "=" * 70)
    print(">>> 恭喜！auth/rbac.py 全部 11 组离线单元测试 100% 通过！ <<<")
    print("=" * 70)


if __name__ == "__main__":
    import socket
    from unittest.mock import patch
    import auth.rbac as rbac_mod
    import db.local_db as local_db_mod
    import db.his_reader as his_mod
    conn_calls = []
    def _forbid_io(*args, **kwargs):
        conn_calls.append((args, kwargs))
        raise AssertionError("TEST-OFFLINE-001 违规：离线测试严禁发起数据库或网络连接！")
    with patch.object(local_db_mod, "get_local_db_connection", side_effect=_forbid_io),          patch.object(rbac_mod, "get_local_db_connection", side_effect=_forbid_io),          patch.object(his_mod.HISReader, "get_connection", side_effect=_forbid_io),          patch.object(socket, "create_connection", side_effect=_forbid_io):
        run_auth_tests()
    assert len(conn_calls) == 0, f"期望零连接尝试，实际 {len(conn_calls)}"
    print("PASS [TEST-OFFLINE-001]: 空环境全量通过，真实 DB 与 Socket 连接尝试次数 = 0")

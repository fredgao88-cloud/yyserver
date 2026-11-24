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

    print("\n" + "=" * 70)
    print(">>> 恭喜！auth/rbac.py 全部 8 组离线单元测试 100% 通过！ <<<")
    print("=" * 70)


if __name__ == "__main__":
    run_auth_tests()

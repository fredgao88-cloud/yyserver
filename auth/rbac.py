# -*- coding: utf-8 -*-
"""
auth/rbac.py — 院内本地服务端鉴权与角色访问控制 (RBAC) 模块
严格遵循《开发设计文档 v1.2》合规红线：
1. 零外部依赖、零云端：PBKDF2-HMAC-SHA256 口令哈希 + HMAC-SHA256 签名令牌，全部标准库本地实现；
2. 患者级接口（判灯核查、在院预警）强制有效令牌 + 角色校验，绝不信任任何请求头自报身份；
3. 审计日志只记录工号与结构化结果，绝不记录口令、令牌或患者敏感信息；
4. 登录失败内存限流：同一工号连续失败 5 次锁定 10 分钟，防口令爆破；
5. 令牌携带过期时间，过期/篡改/伪造一律拒绝（常数时间比较，防时序侧信道）。
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import re
from typing import Any, Dict, List, Optional, Set, Tuple

# 确保能加载项目内部模块
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv

_dotenv_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env"))
if os.path.exists(_dotenv_path):
    load_dotenv(_dotenv_path)

from db.local_db import get_local_db_connection, record_audit_log

# ----------------- 常量与角色定义 -----------------

PBKDF2_ITERATIONS = 200_000  # 口令哈希迭代次数

ROLE_DOCTOR = "DOCTOR"                  # 医生：DIP 规则查询、本人管床患者判灯核查
ROLE_DEPT_HEAD = "DEPT_HEAD"            # 科室主任：DIP 查询、本科室患者判灯核查、本科室预警看板
ROLE_INSURANCE = "INSURANCE_OFFICER"    # 医保办：DIP 查询、全院判灯核查、在院预警看板
ROLE_ADMIN = "ADMIN"                    # 管理员：全部权限 + 账号管理
ALL_ROLES = (ROLE_DOCTOR, ROLE_DEPT_HEAD, ROLE_INSURANCE, ROLE_ADMIN)

# 接口级角色矩阵
PATIENT_QUERY_ROLES = (ROLE_DOCTOR, ROLE_DEPT_HEAD, ROLE_INSURANCE, ROLE_ADMIN)  # 患者判灯核查：全体在岗角色
ALERTS_ROLES = (ROLE_DEPT_HEAD, ROLE_INSURANCE, ROLE_ADMIN)                      # 在院控费预警看板：科室主任、医保办与管理职责

TOKEN_TTL_MINUTES = int(os.getenv("AUTH_TOKEN_TTL_MINUTES", "720"))  # 令牌有效期，默认 12 小时

# 登录失败内存限流（单进程内有效）：operator_id -> (连续失败次数, 锁定截止 epoch)
_login_failures: Dict[str, Tuple[int, float]] = {}
MAX_LOGIN_FAILS = 5
LOCKOUT_SECONDS = 600


# ----------------- 密钥与编码 -----------------

def _auth_secret() -> bytes:
    secret = os.getenv("AUTH_SECRET", "").strip()
    if not secret:
        raise RuntimeError("AUTH_SECRET 未配置：请在 .env 中设置 AUTH_SECRET（强随机串）后再使用鉴权功能")
    return secret.encode("utf-8")


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


# ----------------- 口令哈希（PBKDF2-HMAC-SHA256） -----------------

def hash_password(password: str) -> str:
    """生成 PBKDF2-HMAC-SHA256 口令哈希，格式：pbkdf2_sha256$迭代次数$盐$哈希（均为 base64）"""
    if not password or len(password) < 8:
        raise ValueError("口令长度至少 8 位")
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${_b64url_encode(salt)}${_b64url_encode(dk)}"


def verify_password(password: str, stored_hash: str) -> bool:
    """常数时间校验口令，格式异常一律视为不匹配"""
    try:
        algo, iters_s, salt_b64, hash_b64 = stored_hash.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"),
            _b64url_decode(salt_b64), int(iters_s)
        )
        return hmac.compare_digest(dk, _b64url_decode(hash_b64))
    except Exception:
        return False


# ----------------- 令牌签发与校验（HMAC-SHA256 签名，自包含过期时间） -----------------

def issue_token(operator_id: str, role_type: str, dept_scope: str = "", cred_version: int = 0) -> str:
    """签发 v1.<payload>.<signature> 形式的本地令牌（cv 为凭据版本，账号凭据/权限变更后旧令牌失效）"""
    if role_type not in ALL_ROLES:
        raise ValueError(f"非法角色类型: {role_type}")
    now = int(time.time())
    payload = {
        "sub": operator_id,
        "role": role_type,
        "dept": dept_scope or "",
        "cv": int(cred_version or 0),
        "iat": now,
        "exp": now + TOKEN_TTL_MINUTES * 60
    }
    body = _b64url_encode(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    sig = _b64url_encode(hmac.new(_auth_secret(), f"v1.{body}".encode("ascii"), hashlib.sha256).digest())
    return f"v1.{body}.{sig}"


def verify_token(token: str) -> Optional[Dict[str, Any]]:
    """校验令牌：格式、签名（常数时间）、有效期、角色合法性。任何异常一律返回 None"""
    if not token or not isinstance(token, str):
        return None
    try:
        parts = token.split(".")
        if len(parts) != 3 or parts[0] != "v1":
            return None
        body, sig = parts[1], parts[2]
        expected = _b64url_encode(hmac.new(_auth_secret(), f"v1.{body}".encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(_b64url_decode(body))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None  # 已过期
        if payload.get("role") not in ALL_ROLES:
            return None
        if not payload.get("sub"):
            return None
        return payload
    except Exception:
        return None


def revalidate_operator(payload: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    受保护请求逐次核对数据库当前状态（AUTH-REV-001）：
    - 账号不存在或已停用 → 拒绝；
    - 凭据版本（cv）与数据库不一致 → 拒绝（口令/角色/状态变更后旧令牌立即失效，旧版无 cv 令牌一律需重新登录）；
    - 通过则以数据库当前角色与科室范围为准返回最新身份（降权即时生效）。
    返回 (最新身份, 拒绝原因)；成功时拒绝原因为空串。数据库暂不可用时一律拒绝，绝不放行。
    """
    try:
        op = get_operator(payload.get("sub", ""))
    except Exception:
        return None, "鉴权服务暂不可用"
    if op is None:
        return None, "账号不存在"
    if op.get("status") != "ACTIVE":
        return None, "账号已停用"
    try:
        if int(op.get("cred_version") or 0) != int(payload.get("cv", -1)):
            return None, "凭据已变更，请重新登录"
    except (TypeError, ValueError):
        return None, "凭据已变更，请重新登录"
    return {
        "sub": op["operator_id"],
        "role": op["role_type"],
        "dept": op.get("dept_scope", "") or "",
        "doctor_id": op.get("doctor_id") or "",
        "cv": int(op.get("cred_version") or 0),
        "iat": payload.get("iat"),
        "exp": payload.get("exp"),
    }, ""


def extract_bearer_token(authorization: Optional[str]) -> Optional[str]:
    """从 Authorization 请求头提取 Bearer 令牌"""
    if not authorization:
        return None
    parts = authorization.strip().split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
        return parts[1].strip()
    return None


def role_permitted(role_type: str, allowed_roles: Tuple[str, ...]) -> bool:
    """角色矩阵判定（纯函数，便于离线单测）"""
    return role_type in allowed_roles


# ----------------- 数据库：账号表与操作员管理 -----------------

def ensure_auth_schema() -> None:
    """创建操作员账号表 dbo.sys_operator_account（幂等，并补齐与 DDL sys_user_role 一致字段）"""
    cursor, conn = get_local_db_connection(as_dict=False)
    try:
        cursor.execute("""
        IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'sys_operator_account' AND schema_id = SCHEMA_ID('dbo'))
        CREATE TABLE dbo.sys_operator_account (
            operator_id     NVARCHAR(64)  NOT NULL PRIMARY KEY,
            display_name    NVARCHAR(64)  NOT NULL,
            password_hash   NVARCHAR(256) NOT NULL,
            role_type       NVARCHAR(30)  NOT NULL,
            dept_scope      NVARCHAR(300) NOT NULL CONSTRAINT DF_opacct_dept DEFAULT '',
            dingtalk_userid NVARCHAR(100) NULL,
            can_export_case BIT           NOT NULL CONSTRAINT DF_opacct_export DEFAULT 0,
            status          NVARCHAR(20)  NOT NULL CONSTRAINT DF_opacct_status DEFAULT 'ACTIVE',
            cred_version    INT           NOT NULL CONSTRAINT DF_opacct_credv DEFAULT 0,
            created_at      DATETIME2     NOT NULL CONSTRAINT DF_opacct_created DEFAULT SYSDATETIME(),
            updated_at      DATETIME2     NOT NULL CONSTRAINT DF_opacct_updated DEFAULT SYSDATETIME()
        );
        IF COL_LENGTH('dbo.sys_operator_account', 'dingtalk_userid') IS NULL
            ALTER TABLE dbo.sys_operator_account ADD dingtalk_userid NVARCHAR(100) NULL;
        IF COL_LENGTH('dbo.sys_operator_account', 'can_export_case') IS NULL
            ALTER TABLE dbo.sys_operator_account ADD can_export_case BIT NOT NULL CONSTRAINT DF_opacct_export DEFAULT 0;
        IF COL_LENGTH('dbo.sys_operator_account', 'doctor_id') IS NULL
            ALTER TABLE dbo.sys_operator_account ADD doctor_id NVARCHAR(64) NULL;
        IF COL_LENGTH('dbo.sys_operator_account', 'cred_version') IS NULL
            ALTER TABLE dbo.sys_operator_account ADD cred_version INT NOT NULL CONSTRAINT DF_opacct_credv DEFAULT 0;
        """)
        conn.commit()
    finally:
        conn.close()


def get_operator(operator_id: str) -> Optional[Dict[str, Any]]:
    """按工号查询操作员（含口令哈希，仅限模块内部鉴权使用）"""
    cursor, conn = get_local_db_connection(as_dict=True)
    try:
        cursor.execute(
            "SELECT operator_id, display_name, password_hash, role_type, dept_scope, dingtalk_userid, can_export_case, doctor_id, status, cred_version "
            "FROM dbo.sys_operator_account WHERE operator_id = %s",
            (operator_id,)
        )
        return cursor.fetchone()
    finally:
        conn.close()


def list_operators() -> List[Dict[str, Any]]:
    """列出全部操作员（绝不返回口令哈希）"""
    cursor, conn = get_local_db_connection(as_dict=True)
    try:
        cursor.execute(
            "SELECT operator_id, display_name, role_type, dept_scope, dingtalk_userid, can_export_case, doctor_id, status, updated_at "
            "FROM dbo.sys_operator_account ORDER BY operator_id ASC"
        )
        return cursor.fetchall()
    finally:
        conn.close()


def create_operator(operator_id: str, display_name: str, password: str,
                    role_type: str, dept_scope: str = "", dingtalk_userid: str = "",
                    can_export_case: bool = False, doctor_id: str = "") -> None:
    """创建或重置操作员账号（口令只存哈希，绝不落明文）"""
    operator_id = (operator_id or "").strip()
    if not operator_id or len(operator_id) > 64:
        raise ValueError("工号不能为空且不超过 64 字符")
    if role_type not in ALL_ROLES:
        raise ValueError(f"非法角色类型: {role_type}，可选: {ALL_ROLES}")
    pwd_hash = hash_password(password)
    cursor, conn = get_local_db_connection(as_dict=False)
    try:
        cursor.execute("SELECT 1 FROM dbo.sys_operator_account WHERE operator_id = %s", (operator_id,))
        exists = cursor.fetchone() is not None
        if exists:
            cursor.execute(
                "UPDATE dbo.sys_operator_account SET display_name=%s, password_hash=%s, role_type=%s, "
                "dept_scope=%s, dingtalk_userid=%s, can_export_case=%s, doctor_id=%s, status='ACTIVE', "
                "cred_version=cred_version+1, updated_at=SYSDATETIME() WHERE operator_id=%s",
                (display_name, pwd_hash, role_type, dept_scope or "", dingtalk_userid or None, 1 if can_export_case else 0, doctor_id or None, operator_id)
            )
        else:
            cursor.execute(
                "INSERT INTO dbo.sys_operator_account (operator_id, display_name, password_hash, role_type, dept_scope, dingtalk_userid, can_export_case, doctor_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (operator_id, display_name, pwd_hash, role_type, dept_scope or "", dingtalk_userid or None, 1 if can_export_case else 0, doctor_id or None)
            )
        conn.commit()
    finally:
        conn.close()


def set_operator_status(operator_id: str, status: str) -> bool:
    """停用/启用操作员账号。返回是否命中账号"""
    if status not in ("ACTIVE", "DISABLED"):
        raise ValueError("status 仅支持 ACTIVE / DISABLED")
    cursor, conn = get_local_db_connection(as_dict=False)
    try:
        cursor.execute(
            "UPDATE dbo.sys_operator_account SET status=%s, cred_version=cred_version+1, updated_at=SYSDATETIME() WHERE operator_id=%s",
            (status, operator_id)
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


# ----------------- 登录认证（含失败限流） -----------------

def authenticate(operator_id: str, password: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    校验工号 + 口令。返回 (操作员信息, 拒绝原因)；成功时拒绝原因为空串。
    连续失败 MAX_LOGIN_FAILS 次锁定 LOCKOUT_SECONDS 秒。
    """
    operator_id = (operator_id or "").strip()[:64]
    now = time.time()

    fail_count, locked_until = _login_failures.get(operator_id, (0, 0.0))
    if locked_until > now:
        return None, f"账号已锁定，请 {int(locked_until - now)} 秒后再试"

    op = get_operator(operator_id)
    if op is None or op.get("status") != "ACTIVE" or not verify_password(password or "", op.get("password_hash", "")):
        fail_count += 1
        locked_until = now + LOCKOUT_SECONDS if fail_count >= MAX_LOGIN_FAILS else 0.0
        _login_failures[operator_id] = (fail_count, locked_until)
        return None, "工号或口令错误" if locked_until <= now else f"连续失败 {MAX_LOGIN_FAILS} 次，账号已锁定 {LOCKOUT_SECONDS // 60} 分钟"

    _login_failures.pop(operator_id, None)
    return op, ""


# ----------------- FastAPI 依赖注入（延迟导入 fastapi，离线单测无需安装） -----------------



def resolve_authorized_dept_ids(dept_scope: str, his_ks_rows: Optional[List[Dict[str, Any]]] = None) -> Set[str]:
    """
    AUTH-SCOPE-002：严格解析授权科室 ID 集合，仅返回可信的科室 ID（ksid），绝不混入科室名称。
    若 dept_scope 中包含名称且在 his_ks 中对应多个不同 ID（同名歧义），拒绝该名称映射。
    """
    if not dept_scope:
        return set()
    raw_items = [x.strip() for x in re.split(r'[,;，；]', str(dept_scope)) if x.strip()]
    if not raw_items:
        return set()

    rows = his_ks_rows
    if rows is None:
        try:
            from db.his_reader import HISReader
            r = HISReader()
            conn = r.get_connection()
            cur = conn.cursor(as_dict=True)
            try:
                cur.execute("SELECT ksid, ksname FROM dbo.his_ks WHERE ksid IS NOT NULL")
                rows = cur.fetchall()
            finally:
                conn.close()
        except Exception:
            rows = None

    if rows is None:
        # AUTH-SCOPE-002：科室目录不可用时一律拒绝，绝不扩大或猜测授权范围
        return set()

    id_set = set()
    name_to_ids: Dict[str, set] = {}
    clean_name_to_ids: Dict[str, set] = {}
    for row in rows:
        ks_id_str = str(row.get("ksid") if row.get("ksid") is not None else "").strip()
        if not ks_id_str:
            continue
        id_set.add(ks_id_str)
        ks_name = (row.get("ksname") or "").strip()
        if ks_name:
            name_to_ids.setdefault(ks_name, set()).add(ks_id_str)
            ks_clean = re.sub(r'^\d+', '', ks_name).strip()
            if ks_clean:
                clean_name_to_ids.setdefault(ks_clean, set()).add(ks_id_str)

    dept_ids = set()
    for item in raw_items:
        if item in id_set:
            dept_ids.add(item)
        elif item in name_to_ids:
            if len(name_to_ids[item]) == 1:
                dept_ids.update(name_to_ids[item])
        elif item in clean_name_to_ids:
            if len(clean_name_to_ids[item]) == 1:
                dept_ids.update(clean_name_to_ids[item])
    return dept_ids


def get_authorized_dept_ids(dept_scope: str) -> Set[str]:
    """根据操作员配置的科室范围 (dept_scope)，严格解析授权科室 ID 集合。"""
    return resolve_authorized_dept_ids(dept_scope)

def require_roles(*allowed_roles: str):
    """患者级/受保护接口依赖：强制有效令牌 + 角色矩阵校验，拒绝即写审计日志"""
    from fastapi import Header, HTTPException

    async def _dependency(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
        token = extract_bearer_token(authorization)
        payload = verify_token(token) if token else None
        if payload is None:
            record_audit_log(
                operator_id="anonymous", role_type="UNKNOWN", channel="API",
                action_type="AUTH_CHECK", target_ref="TOKEN:INVALID", auth_result="DENIED"
            )
            raise HTTPException(status_code=401, detail="未认证或令牌已过期：请先调用 /api/auth/login 获取访问令牌")
        # AUTH-REV-001：逐次核对数据库当前在岗状态、角色与凭据版本，拒绝发生在任何业务数据读取之前
        fresh, reason = revalidate_operator(payload)
        if fresh is None:
            record_audit_log(
                operator_id=payload["sub"], role_type=payload["role"], channel="API",
                action_type="AUTH_CHECK", target_ref="TOKEN:REVALIDATE_DENIED", auth_result="DENIED"
            )
            raise HTTPException(status_code=401, detail=f"登录状态已失效：{reason}，请重新登录")
        if not role_permitted(fresh["role"], allowed_roles):
            record_audit_log(
                operator_id=fresh["sub"], role_type=fresh["role"], channel="API",
                action_type="AUTH_CHECK", target_ref="ROLE:DENIED", auth_result="DENIED"
            )
            raise HTTPException(status_code=403, detail="权限不足：当前角色无权访问该接口")
        return fresh

    return _dependency


def operator_from_optional_token(authorization: Optional[str]) -> Dict[str, Any]:
    """
    开放接口（DIP 规则查询、聊天问答）的可选身份解析：
    有有效令牌则采用令牌身份；无令牌按 anonymous 记录审计，绝不信任任何自报身份请求头。
    """
    token = extract_bearer_token(authorization)
    payload = verify_token(token) if token else None
    if payload:
        # AUTH-REV-001：可选身份同样逐次核对数据库；撤权/降权后一律降级为 anonymous
        fresh, _reason = revalidate_operator(payload)
        if fresh is not None:
            return {"sub": fresh["sub"], "role": fresh["role"], "authenticated": True}
    return {"sub": "anonymous", "role": ROLE_DOCTOR, "authenticated": False}


# ----------------- 运维 CLI -----------------

def _cli() -> None:
    parser = argparse.ArgumentParser(description="院内 Agent 操作员账号管理（本地运维工具）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ensure-schema", help="创建账号表（幂等）")

    p_create = sub.add_parser("create-operator", help="创建或重置操作员账号")
    p_create.add_argument("--id", required=True, help="工号")
    p_create.add_argument("--name", required=True, help="姓名/显示名")
    p_create.add_argument("--role", required=True, choices=list(ALL_ROLES), help="角色")
    p_create.add_argument("--dept", default="", help="科室范围")
    p_create.add_argument("--dingtalk-id", default="", help="钉钉 userid 映射")
    p_create.add_argument("--can-export", action="store_true", default=False, help="是否允许导出完整病例")
    p_create.add_argument("--password", default=None, help="初始口令（缺省时随机生成并打印一次）")

    p_status = sub.add_parser("set-status", help="停用/启用账号")
    p_status.add_argument("--id", required=True)
    p_status.add_argument("--status", required=True, choices=["ACTIVE", "DISABLED"])

    sub.add_parser("list", help="列出全部账号（不含口令哈希）")

    args = parser.parse_args()

    if args.cmd == "ensure-schema":
        ensure_auth_schema()
        print("OK: dbo.sys_operator_account 已就绪")
    elif args.cmd == "create-operator":
        ensure_auth_schema()
        password = args.password or secrets.token_urlsafe(12)
        create_operator(args.id, args.name, password, args.role, args.dept, dingtalk_userid=args.dingtalk_id, can_export_case=args.can_export)
        print(f"OK: 账号 {args.id} ({args.role}) 已创建/重置")
        if args.password is None:
            # 随机口令只在此打印一次，请立即记录并妥善保管
            print(f"INITIAL_PASSWORD={password}")
    elif args.cmd == "set-status":
        ensure_auth_schema()
        ok = set_operator_status(args.id, args.status)
        print(f"OK: {args.id} -> {args.status}" if ok else f"NOT_FOUND: {args.id}")
    elif args.cmd == "list":
        ensure_auth_schema()
        for op in list_operators():
            print(f"{op['operator_id']}\t{op['display_name']}\t{op['role_type']}\t{op['dept_scope']}\t{op['status']}")


if __name__ == "__main__":
    _cli()

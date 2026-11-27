# -*- coding: utf-8 -*-
"""
SNAP-QUERY-001 隔离 SQL Server 数据库实测验收 (tests/test_snap_query_isolated_db.py)

遵循 TEST-ISO-001 隔离原则：
- 仅当显式配置 SNAP_TEST_DB_HOST / SNAP_TEST_DB_USER / SNAP_TEST_DB_PASS / SNAP_TEST_DB_NAME
  且目标库名不在共享/生产/系统库拒绝名单时执行；否则安全跳过 (exit 0)，零写入。
- 每轮创建一次性隔离库 rxt_snap_<随机>，在 finally 中强制清理销毁。

验收内容（对应 Issue #2 · SNAP-QUERY-001）：
1. 构造无 dept_id 列的旧版 dbo.inpatient_dip_snapshot 及无 doctor_id 列的旧版 dbo.sys_operator_account，
   灌入历史合成快照行（升级后 dept_id 为 NULL）。
2. 提取 sql/01_init_his_agent_db.sql 幂等升级语句及 auth.rbac.ensure_auth_schema()，
   连续执行两次升级（验证幂等性与历史 NULL 科室列保留）。
3. 在仅存在历史 NULL 科室快照时验证：管理员可见全集，科主任不可见 NULL 科室行。
4. 写入包含同名不同 ID（101/102 同名“综合科”）与 NULL 科室的新快照批次，
   通过真实 api.main.get_inpatient_alerts + 真实 SQL Server 游标验证：
   - 管理员看到预期全集 {'VISIT_101', 'VISIT_102', 'VISIT_NULL'}；
   - 科主任（dept_scope='101'）严格只看到授权就诊 ID {'VISIT_101'}；
   - 同名歧义配置（dept_scope='综合科'）返回 HTTP 403。
"""
import asyncio
import os
import re
import sys
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _load_env_fallback() -> None:
    env_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env"))
    if os.path.exists(env_path):
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path)
        except ImportError:
            pass


def _extract_upgrade_blocks(sql_text: str):
    """提取 sql/01_init_his_agent_db.sql 中两段幂等 ALTER TABLE 升级块"""
    blocks = []
    for marker in (
        "COL_LENGTH('dbo.inpatient_dip_snapshot', 'dept_id') IS NULL",
        "COL_LENGTH('dbo.sys_operator_account', 'doctor_id') IS NULL",
    ):
        pattern = re.compile(
            r"IF\s+OBJECT_ID\([^\n]*?\)\s+IS\s+NOT\s+NULL\s+AND\s+"
            + re.escape(marker)
            + r"\s+BEGIN\s+.*?\s+END",
            re.S,
        )
        m = pattern.search(sql_text)
        assert m is not None, f"未在建表脚本中找到幂等升级块: {marker}"
        blocks.append(m.group(0))
    return blocks


def run_snap_query_isolated_db() -> None:
    _load_env_fallback()

    host = (os.environ.get("SNAP_TEST_DB_HOST") or "").strip()
    port = int(os.environ.get("SNAP_TEST_DB_PORT") or "1433")
    user = (os.environ.get("SNAP_TEST_DB_USER") or "").strip()
    password = (os.environ.get("SNAP_TEST_DB_PASS") or "").strip()
    bootstrap_db = (os.environ.get("SNAP_TEST_DB_NAME") or "").strip()

    if not (host and user and password and bootstrap_db):
        print("[SKIP] TEST-ISO-001 (SNAP-QUERY-001): 未配置 SNAP_TEST_DB_* 隔离实例，跳过真实数据库验收并保证零写入")
        return

    if bootstrap_db.lower() in ("his", "hisdb", "master", "model", "msdb"):
        raise PermissionError(
            f"SNAP-QUERY-001 拒绝连接共享/生产/系统库 SNAP_TEST_DB_NAME={bootstrap_db!r}"
        )

    try:
        import pymssql
    except ImportError:
        print("[SKIP] SNAP-QUERY-001: pymssql 不可用，跳过真实数据库验收")
        return

    os.environ.setdefault("AUTH_SECRET", "snap-query-isolated-test-secret-0123456789")
    os.environ.setdefault("HIS_PASS", "snap-test-synthetic-his-pass")

    from fastapi import HTTPException
    import api.main as api_main
    import auth.rbac as rbac_mod

    sql_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "sql", "01_init_his_agent_db.sql"))
    with open(sql_path, "r", encoding="utf-8") as f:
        upgrade_blocks = _extract_upgrade_blocks(f.read())

    scratch_db = "rxt_snap_" + uuid.uuid4().hex[:8]
    admin_conn = pymssql.connect(
        server=host, port=port, user=user, password=password,
        database=bootstrap_db, autocommit=True, login_timeout=10, timeout=30
    )
    admin_cur = admin_conn.cursor()
    admin_cur.execute(f"CREATE DATABASE [{scratch_db}]")
    print(f"[SNAP-QUERY-001] 已创建一次性隔离库: {scratch_db}")

    def _scratch_conn(as_dict: bool = True):
        conn = pymssql.connect(
            server=host, port=port, user=user, password=password,
            database=scratch_db, charset="utf8", login_timeout=10, timeout=30
        )
        return conn.cursor(as_dict=as_dict), conn

    try:
        # 1) 构建旧版表结构（无 dept_id 与 doctor_id）并写入历史快照
        c, conn = _scratch_conn(as_dict=False)
        try:
            c.execute("""
            CREATE TABLE dbo.inpatient_dip_snapshot (
                snapshot_id       BIGINT IDENTITY(1, 1) PRIMARY KEY,
                batch_id          NVARCHAR(64)   NOT NULL,
                visit_id          NVARCHAR(64)   NOT NULL,
                ward_name         NVARCHAR(50)   NOT NULL,
                bed_no            NVARCHAR(30)   NOT NULL,
                dept_name         NVARCHAR(50)   NOT NULL,
                doctor_id         NVARCHAR(50)   NOT NULL,
                age               INT            NULL,
                ins_type          NVARCHAR(20)   NOT NULL,
                primary_icd       NVARCHAR(50)   NULL,
                matched_rule_id   INT            NULL,
                rule_version      NVARCHAR(50)   NULL,
                current_fee       DECIMAL(14, 4) NOT NULL DEFAULT 0,
                target_base       DECIMAL(14, 4) NULL,
                fee_ratio         DECIMAL(12, 6) NULL,
                alert_level       NVARCHAR(20)   NOT NULL,
                pending_reason    NVARCHAR(200)  NULL,
                discharge_review  BIT            NOT NULL DEFAULT 0,
                op_audit_status   NVARCHAR(30)   NULL,
                data_cutoff_time  DATETIME2      NOT NULL,
                synced_at         DATETIME2      NOT NULL DEFAULT SYSDATETIME()
            );
            CREATE UNIQUE INDEX UX_snapshot_batch_visit ON dbo.inpatient_dip_snapshot(batch_id, visit_id);

            CREATE TABLE dbo.sys_operator_account (
                operator_id     NVARCHAR(64)   PRIMARY KEY,
                display_name    NVARCHAR(64)   NOT NULL,
                password_hash   NVARCHAR(256)  NOT NULL,
                role_type       NVARCHAR(30)   NOT NULL,
                dept_scope      NVARCHAR(300)  NOT NULL DEFAULT N'',
                status          NVARCHAR(20)   NOT NULL DEFAULT N'ACTIVE',
                created_at      DATETIME2      NOT NULL DEFAULT SYSDATETIME(),
                updated_at      DATETIME2      NOT NULL DEFAULT SYSDATETIME()
            );

            CREATE TABLE dbo.agent_audit_log (
                log_id          BIGINT IDENTITY(1, 1) PRIMARY KEY,
                operator_id     NVARCHAR(64)   NOT NULL,
                role_type       NVARCHAR(30)   NOT NULL,
                channel         NVARCHAR(30)   NOT NULL,
                action_type     NVARCHAR(50)   NOT NULL,
                target_ref      NVARCHAR(100)  NULL,
                auth_result     NVARCHAR(20)   NOT NULL,
                rule_version    NVARCHAR(50)   NULL,
                batch_id        NVARCHAR(64)   NULL,
                created_at      DATETIME2      NOT NULL DEFAULT SYSDATETIME()
            );
            """)
            t_legacy = datetime.now() - timedelta(minutes=10)
            c.execute(
                "INSERT INTO dbo.inpatient_dip_snapshot "
                "(batch_id, visit_id, ward_name, bed_no, dept_name, doctor_id, age, ins_type, primary_icd, "
                " current_fee, target_base, fee_ratio, alert_level, data_cutoff_time, synced_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                ("BATCH_LEGACY_001", "VISIT_LEGACY_NULL", "老病区", "01", "综合科", "doc01",
                 66, "职工", "M17.9", 2100.0, 2790.9, 0.752445, "GREEN", t_legacy, t_legacy)
            )
            conn.commit()
        finally:
            conn.close()

        # 2) 连续升级两次（01_init_his_agent_db.sql 幂等升级脚本 + ensure_auth_schema）
        with patch.object(rbac_mod, "get_local_db_connection", side_effect=_scratch_conn):
            for round_idx in (1, 2):
                c, conn = _scratch_conn(as_dict=False)
                try:
                    for blk in upgrade_blocks:
                        c.execute(blk)
                    conn.commit()
                finally:
                    conn.close()
                rbac_mod.ensure_auth_schema()
                print(f"PASS [Upgrade Round {round_idx}]: 幂等升级第 {round_idx} 次成功")

        # 校验升级后列存在且历史行 dept_id 为 NULL
        c, conn = _scratch_conn(as_dict=True)
        try:
            c.execute(
                "SELECT COL_LENGTH('dbo.inpatient_dip_snapshot', 'dept_id') AS snap_dept_len, "
                "COL_LENGTH('dbo.sys_operator_account', 'doctor_id') AS acct_doc_len"
            )
            col_info = c.fetchone()
            assert col_info["snap_dept_len"] is not None and col_info["acct_doc_len"] is not None

            c.execute("SELECT visit_id, dept_id FROM dbo.inpatient_dip_snapshot WHERE batch_id = 'BATCH_LEGACY_001'")
            legacy_row = c.fetchone()
            assert legacy_row["visit_id"] == "VISIT_LEGACY_NULL" and legacy_row["dept_id"] is None

            # 写入合成操作员账号（无任何真实密码）
            pwd_hash = rbac_mod.hash_password("Synth!Pass123")
            for oid, name, role, scope in [
                ("admin01", "合成管理员", rbac_mod.ROLE_ADMIN, ""),
                ("dh101", "合成主任101", rbac_mod.ROLE_DEPT_HEAD, "101"),
                ("dh_ambig", "合成歧义主任", rbac_mod.ROLE_DEPT_HEAD, "综合科"),
            ]:
                c.execute(
                    "INSERT INTO dbo.sys_operator_account (operator_id, display_name, password_hash, role_type, dept_scope, status) "
                    "VALUES (%s, %s, %s, %s, %s, 'ACTIVE')",
                    (oid, name, pwd_hash, role, scope)
                )
            conn.commit()
        finally:
            conn.close()

        his_ks_mock = [
            {"ksid": 101, "ksname": "综合科"},
            {"ksid": 102, "ksname": "综合科"},
        ]

        with patch.object(api_main, "get_local_db_connection", side_effect=_scratch_conn), \
             patch.object(rbac_mod, "get_local_db_connection", side_effect=_scratch_conn), \
             patch.object(api_main, "record_audit_log"), \
             patch.object(api_main, "get_authorized_dept_ids", side_effect=lambda s: rbac_mod.resolve_authorized_dept_ids(s, his_ks_rows=his_ks_mock)):

            # 3) 仅历史 NULL 科室快照存在时：管理员可见，主任不可见
            res_legacy_admin = asyncio.run(api_main.get_inpatient_alerts(dept=None, operator={"sub": "admin01", "role": "ADMIN", "dept": ""}))
            assert {i["zyh"] for i in res_legacy_admin["items"]} == {"VISIT_LEGACY_NULL"}

            res_legacy_dh = asyncio.run(api_main.get_inpatient_alerts(dept=None, operator={"sub": "dh101", "role": "DEPT_HEAD", "dept": "101"}))
            assert res_legacy_dh["items"] == [], f"历史 NULL 科室行不应暴露给科主任，实际 {res_legacy_dh['items']}"
            print("PASS [Legacy Batch Visibility]: 连续两次升级后，历史 NULL 科室快照仅管理员可见、主任不可见")

            # 4) 写入含同名不同 ID (101/102) 及 NULL 科室的新快照批次
            t_new = datetime.now()
            c, conn = _scratch_conn(as_dict=False)
            try:
                for vid, did, dname, fee, level in [
                    ("VISIT_101", "101", "综合科", 2600.0, "YELLOW"),
                    ("VISIT_102", "102", "综合科", 3200.0, "RED"),
                    ("VISIT_NULL", None, "综合科", 1900.0, "GREEN"),
                ]:
                    c.execute(
                        "INSERT INTO dbo.inpatient_dip_snapshot "
                        "(batch_id, visit_id, ward_name, bed_no, dept_id, dept_name, doctor_id, age, ins_type, primary_icd, "
                        " current_fee, target_base, fee_ratio, alert_level, data_cutoff_time, synced_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        ("BATCH_UPGRADED_002", vid, "合成病区", "02", did, dname, "doc01",
                         68, "职工", "M17.9", fee, 2790.9, fee / 2790.9, level, t_new, t_new)
                    )
                conn.commit()
            finally:
                conn.close()

            # 5) 断言管理员看到预期全集，主任只看到授权就诊 ID，同名歧义拒绝 403
            res_admin = asyncio.run(api_main.get_inpatient_alerts(dept=None, operator={"sub": "admin01", "role": "ADMIN", "dept": ""}))
            admin_ids = {i["zyh"] for i in res_admin["items"]}
            assert admin_ids == {"VISIT_101", "VISIT_102", "VISIT_NULL"}, f"管理员期望全集，实际 {admin_ids}"

            res_dh = asyncio.run(api_main.get_inpatient_alerts(dept=None, operator={"sub": "dh101", "role": "DEPT_HEAD", "dept": "101"}))
            dh_ids = {i["zyh"] for i in res_dh["items"]}
            assert dh_ids == {"VISIT_101"}, f"科主任期望仅 {'VISIT_101'}，实际 {dh_ids}"
            assert res_dh["items"][0]["dept_id"] == "101"

            try:
                asyncio.run(api_main.get_inpatient_alerts(dept=None, operator={"sub": "dh_ambig", "role": "DEPT_HEAD", "dept": "综合科"}))
                raise AssertionError("同名歧义科室配置必须被拒绝 403")
            except HTTPException as exc:
                assert exc.status_code == 403

            print(f"PASS [SNAP-QUERY-001 Isolated DB]: 管理员全集={sorted(admin_ids)}，主任101={sorted(dh_ids)}，歧义=403")

    finally:
        try:
            admin_cur.execute(f"ALTER DATABASE [{scratch_db}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
        except Exception:
            pass
        admin_cur.execute(f"DROP DATABASE [{scratch_db}]")
        admin_conn.close()
        print(f"[SNAP-QUERY-001] 已销毁一次性隔离库: {scratch_db}")


if __name__ == "__main__":
    print("=" * 70)
    print(">>> SNAP-QUERY-001 隔离 SQL Server 数据库验收 <<<")
    print("=" * 70)
    run_snap_query_isolated_db()

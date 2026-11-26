# -*- coding: utf-8 -*-
"""
离线/无写入全链路鉴权与 HIS 读取器单元测试 (tests/test_auth_scope_offline.py)
覆盖：
  1. HIS-FIXTURE-001: 生产 HISReader 对所有编号走统一 SQL 查询（无 SYNTH_* 硬编码分支），模拟游标验证参数与空记录拒绝
  2. AUTH-SCOPE-001: api.main.check_patient_dip 显式 doctor_id 映射严格优先、防工号碰撞、重置后旧关系拒绝、旧账号兼容
  3. AUTH-SCOPE-002 & SNAP-QUERY-001: api.main.get_inpatient_alerts 同名不同 ID 科室严格按 dept_id 隔离、缺失 dept_id 历史行对科主任不可见、管理员全量可见
"""
import asyncio
import os
import sys
import uuid
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

os.environ.setdefault("AUTH_SECRET", "unit-test-only-secret-do-not-use-in-prod")
os.environ.setdefault("HIS_PASS", "offline-synthetic-his-pass")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import HTTPException
import api.main as api_main
from db.his_reader import HISReader
import auth.rbac as rbac_mod


class _FakeCursor:
    def __init__(self, row=None):
        self.row = row
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self.row


class _FakeConn:
    def __init__(self, cursor):
        self._cur = cursor
        self.closed = False

    def cursor(self, as_dict=False):
        return self._cur

    def close(self):
        self.closed = True


def test_his_fixture_removed():
    # 1. 验证生产读取器对 SYNTH_DOC_ATTEND 与普通编号走完全相同的 SQL 查询，无硬编码分支
    sample_row = {
        "zyh": "SYNTH_DOC_ATTEND",
        "patient_name": "测试患者",
        "gender": "男",
        "age": 68,
        "current_dept_id": "4",
        "current_dept": "01中医科",
        "current_bed": "01",
        "admission_time": datetime(2026, 11, 24, 8, 0, 0),
        "discharge_time": None,
        "attending_doctor_id": "DOC_A_HIS",
        "attending_doctor": "测试医生",
        "dept_director_id": "dh01",
        "dept_director": "测试主任",
        "admission_diag_code": "M17.9",
        "admission_diag_name": "未特指的膝关节病",
        "ylzffs": "1",
        "insurance_type": "职工",
    }
    cur_ok = _FakeCursor(row=sample_row)
    conn_ok = _FakeConn(cur_ok)
    reader = HISReader()
    with patch.object(reader, "get_connection", return_value=conn_ok):
        res = reader.get_patient_realtime_admission("SYNTH_DOC_ATTEND")
        assert res is not None and res["zyh"] == "SYNTH_DOC_ATTEND"
        assert res["attending_doctor_id"] == "DOC_A_HIS"
        assert len(cur_ok.executed) == 1
        sql_str, params = cur_ok.executed[0]
        assert "sy.cysj IS NULL" in sql_str and "jz.lsh IS NULL" in sql_str
        assert "WHERE sy.lsh = %s" in sql_str
        assert params == ("SYNTH_DOC_ATTEND",)
        assert conn_ok.closed is True

    # 2. 空记录返回 None（调用方一律拒绝）
    cur_empty = _FakeCursor(row=None)
    conn_empty = _FakeConn(cur_empty)
    with patch.object(reader, "get_connection", return_value=conn_empty):
        res_empty = reader.get_patient_realtime_admission("NON_EXISTENT_999")
        assert res_empty is None
        assert len(cur_empty.executed) == 1
        assert conn_empty.closed is True

    print("PASS [HIS-FIXTURE-001]: 生产读取器无 SYNTH_* 硬编码，统一走带参数在院 SQL 查询，空记录返回 None")


def test_auth_scope_001_doctor_precedence():
    # 两个虚构医生：doc_a 的登录名恰等于 doc_b 的 HIS 工号，验证显式映射严格优先且不发生工号碰撞
    operators = {
        "DOC_B_HIS": {"operator_id": "DOC_B_HIS", "role_type": "DOCTOR", "doctor_id": "DOC_A_HIS", "status": "ACTIVE", "cred_version": 1},
        "doc_b": {"operator_id": "doc_b", "role_type": "DOCTOR", "doctor_id": "DOC_B_HIS", "status": "ACTIVE", "cred_version": 1},
        "LEGACY_DOC": {"operator_id": "LEGACY_DOC", "role_type": "DOCTOR", "doctor_id": None, "status": "ACTIVE", "cred_version": 1},
    }
    admissions = {
        "PAT_A": {"zyh": "PAT_A", "attending_doctor_id": "DOC_A_HIS", "current_dept_id": "4", "current_dept": "01中医科"},
        "PAT_B": {"zyh": "PAT_B", "attending_doctor_id": "DOC_B_HIS", "current_dept_id": "4", "current_dept": "01中医科"},
        "PAT_LEG": {"zyh": "PAT_LEG", "attending_doctor_id": "LEGACY_DOC", "current_dept_id": "4", "current_dept": "01中医科"},
    }

    class _MockReader:
        def get_patient_realtime_admission(self, pid):
            return admissions.get(pid)

    req_a = api_main.CheckPatientDipRequest(
        patient_id="PAT_A", disease="M17.9", current_cost=-1.0, age=68, ins_type="职工", operation="保守治疗"
    )
    req_b = api_main.CheckPatientDipRequest(
        patient_id="PAT_B", disease="M17.9", current_cost=-1.0, age=68, ins_type="职工", operation="保守治疗"
    )
    req_leg = api_main.CheckPatientDipRequest(
        patient_id="PAT_LEG", disease="M17.9", current_cost=-1.0, age=68, ins_type="职工", operation="保守治疗"
    )
    req_missing = api_main.CheckPatientDipRequest(
        patient_id="PAT_NONE", disease="M17.9", current_cost=-1.0, age=68, ins_type="职工", operation="保守治疗"
    )

    op_a = {"sub": "DOC_B_HIS", "role": "DOCTOR", "dept": "中医科"}
    op_leg = {"sub": "LEGACY_DOC", "role": "DOCTOR", "dept": "中医科"}

    with patch.object(api_main, "HISReader", return_value=_MockReader()), \
         patch.object(api_main, "get_operator", side_effect=lambda oid: operators.get(oid)), \
         patch.object(api_main, "record_audit_log"):

        # 1) doc_a 访问本人管床 PAT_A -> 放行（进入负费用拦截返回 PENDING）
        res_ok = asyncio.run(api_main.check_patient_dip(req_a, operator=op_a))
        assert res_ok["status"] == "PENDING" and res_ok["pending_reason"] == "NEGATIVE_FEE_UNRECONCILED"

        # 2) doc_a 访问 PAT_B（即使 doc_a 登录名 == DOC_B_HIS）-> 拒绝 403！
        try:
            asyncio.run(api_main.check_patient_dip(req_b, operator=op_a))
            raise AssertionError("登录名碰撞他人工号必须被拒绝")
        except HTTPException as e:
            assert e.status_code == 403

        # 3) 重置 doc_a 的 doctor_id 为 DOC_A_NEW 后，旧管床 PAT_A 必须拒绝 403！
        operators["DOC_B_HIS"]["doctor_id"] = "DOC_A_NEW"
        try:
            asyncio.run(api_main.check_patient_dip(req_a, operator=op_a))
            raise AssertionError("重置映射后旧管床关系必须拒绝")
        except HTTPException as e:
            assert e.status_code == 403

        # 4) 旧账号无显式 doctor_id 时兼容 operator_id -> 放行
        res_leg = asyncio.run(api_main.check_patient_dip(req_leg, operator=op_leg))
        assert res_leg["status"] == "PENDING" and res_leg["pending_reason"] == "NEGATIVE_FEE_UNRECONCILED"

        # 5) 空记录一律拒绝 403
        try:
            asyncio.run(api_main.check_patient_dip(req_missing, operator=op_a))
            raise AssertionError("空记录必须拒绝 403")
        except HTTPException as e:
            assert e.status_code == 403

        # 6) 保留虚构在院记录，但操作员记录消失 (get_operator 返回 None) -> 即使登录名等于管床号 DOC_A_HIS 也一律拒绝 403
        op_ghost = {"sub": "DOC_A_HIS", "role": "DOCTOR", "dept": "中医科"}
        req_ghost_pat = api_main.CheckPatientDipRequest(
            patient_id="PAT_A", disease="M17.9", current_cost=-1.0, age=68, ins_type="职工", operation="保守治疗"
        )
        try:
            asyncio.run(api_main.check_patient_dip(req_ghost_pat, operator=op_ghost))
            raise AssertionError("不存在的操作员即使有在院记录也必须拒绝 403")
        except HTTPException as e:
            assert e.status_code == 403

    print("PASS [AUTH-SCOPE-001]: 显式映射防工号碰撞、重置后旧关系拒绝、操作员消失拒绝、旧账号兼容及空记录拒绝全部通过")


def test_auth_scope_002_and_snap_query_001():
    # 同名不同 ID 两个科室 + 缺失 dept_id 历史行
    snapshot_rows = [
        {"visit_id": "VISIT_101", "ward_name": "综合一区", "bed_no": "01", "dept_id": "101", "dept_name": "综合科",
         "doctor_id": "D1", "age": 65, "ins_type": "职工", "primary_icd": "M17.9", "matched_rule_id": 85,
         "current_fee": Decimal("2500"), "target_base": Decimal("2790.9"), "fee_ratio": Decimal("0.895"),
         "alert_level": "GREEN", "pending_reason": None, "data_cutoff_time": datetime(2026, 11, 24, 12, 0, 0)},
        {"visit_id": "VISIT_102", "ward_name": "综合二区", "bed_no": "02", "dept_id": "102", "dept_name": "综合科",
         "doctor_id": "D2", "age": 65, "ins_type": "职工", "primary_icd": "M17.9", "matched_rule_id": 85,
         "current_fee": Decimal("2500"), "target_base": Decimal("2790.9"), "fee_ratio": Decimal("0.895"),
         "alert_level": "GREEN", "pending_reason": None, "data_cutoff_time": datetime(2026, 11, 24, 12, 0, 0)},
        {"visit_id": "VISIT_NULL", "ward_name": "历史病区", "bed_no": "03", "dept_id": None, "dept_name": "综合科",
         "doctor_id": "D3", "age": 65, "ins_type": "职工", "primary_icd": "M17.9", "matched_rule_id": 85,
         "current_fee": Decimal("2500"), "target_base": Decimal("2790.9"), "fee_ratio": Decimal("0.895"),
         "alert_level": "GREEN", "pending_reason": None, "data_cutoff_time": datetime(2026, 11, 24, 12, 0, 0)},
    ]

    class _SnapCursor:
        def __init__(self):
            self._rows = []

        def execute(self, sql, params=None):
            if "SELECT TOP 1 batch_id" in sql:
                self._rows = [{"batch_id": "BATCH_TEST", "data_cutoff_time": datetime(2026, 11, 24, 12, 0, 0), "synced_at": datetime(2026, 11, 24, 12, 0, 0)}]
            elif "FROM dbo.inpatient_dip_snapshot" in sql:
                if "AND dept_id IN" in sql:
                    allowed = set(params[1:])
                    self._rows = [r for r in snapshot_rows if r["dept_id"] in allowed]
                else:
                    self._rows = list(snapshot_rows)
            else:
                self._rows = []

        def fetchone(self):
            return self._rows[0] if self._rows else None

        def fetchall(self):
            return self._rows

    class _SnapConn:
        def cursor(self, as_dict=False):
            return _SnapCursor()
        def close(self):
            pass

    his_ks_mock = [
        {"ksid": 101, "ksname": "综合科"},
        {"ksid": 102, "ksname": "综合科"},
    ]

    alert_operators = {
        "dh101": {"operator_id": "dh101", "role_type": "DEPT_HEAD", "dept_scope": "101", "status": "ACTIVE", "cred_version": 1},
        "dh_ambig": {"operator_id": "dh_ambig", "role_type": "DEPT_HEAD", "dept_scope": "综合科", "status": "ACTIVE", "cred_version": 1},
        "admin01": {"operator_id": "admin01", "role_type": "ADMIN", "dept_scope": "", "status": "ACTIVE", "cred_version": 1},
    }

    with patch.object(api_main, "get_local_db_connection", return_value=(_SnapCursor(), _SnapConn())), \
         patch.object(api_main, "get_operator", side_effect=lambda oid: alert_operators.get(oid)), \
         patch.object(api_main, "get_authorized_dept_ids", side_effect=lambda s: rbac_mod.resolve_authorized_dept_ids(s, his_ks_rows=his_ks_mock)), \
         patch.object(api_main, "record_audit_log"):

        # 1) 科主任仅授权 ID 101 -> 只能看到 VISIT_101（严禁混入同名 102 与缺失 dept_id 的 VISIT_NULL）
        op_dh_101 = {"sub": "dh101", "role": "DEPT_HEAD", "dept": "101"}
        res_101 = asyncio.run(api_main.get_inpatient_alerts(dept=None, operator=op_dh_101))
        got_ids = {itm["zyh"] for itm in res_101["items"]}
        assert got_ids == {"VISIT_101"}, f"期望仅返回 {{'VISIT_101'}}，实际 {got_ids}"
        assert res_101["items"][0]["dept_id"] == "101"

        # 2) 科主任配置同名歧义名称“综合科” -> 映射不明确，拒绝 403
        op_dh_ambig = {"sub": "dh_ambig", "role": "DEPT_HEAD", "dept": "综合科"}
        try:
            asyncio.run(api_main.get_inpatient_alerts(dept=None, operator=op_dh_ambig))
            raise AssertionError("同名歧义科室配置必须被拒绝")
        except HTTPException as e:
            assert e.status_code == 403

        # 3) 管理员可查看全量（含缺失 dept_id 的历史行）
        op_admin = {"sub": "admin01", "role": "ADMIN", "dept": ""}
        res_admin = asyncio.run(api_main.get_inpatient_alerts(dept=None, operator=op_admin))
        assert {itm["zyh"] for itm in res_admin["items"]} == {"VISIT_101", "VISIT_102", "VISIT_NULL"}

    # 4) 验证正式建表升级脚本包含幂等 dept_id 与 doctor_id 升级路径
    sql_path = os.path.join(os.path.dirname(__file__), "..", "sql", "01_init_his_agent_db.sql")
    with open(sql_path, "r", encoding="utf-8") as f:
        sql_txt = f.read()
    assert "COL_LENGTH('dbo.inpatient_dip_snapshot', 'dept_id') IS NULL" in sql_txt
    assert "COL_LENGTH('dbo.sys_operator_account', 'doctor_id') IS NULL" in sql_txt

    print("PASS [AUTH-SCOPE-002 & SNAP-QUERY-001]: 同名不同 ID 严格隔离、历史无 ID 行拒绝及幂等升级路径验证通过")


from contextlib import contextmanager
import socket as _socket_mod
import db.local_db as _local_db_mod


@contextmanager
def offline_connection_guard():
    """TEST-OFFLINE-001：共用离线禁连上下文（pytest autouse fixture 与 __main__ 共用）"""
    conn_calls = []

    def _forbid_db(*args, **kwargs):
        conn_calls.append((args, kwargs))
        raise AssertionError("TEST-OFFLINE-001 违规：离线测试严禁打开真实数据库连接或网络！")

    with patch.object(_local_db_mod, "get_local_db_connection", side_effect=_forbid_db), \
         patch.object(rbac_mod, "get_local_db_connection", side_effect=_forbid_db), \
         patch.object(api_main, "get_local_db_connection", side_effect=_forbid_db), \
         patch.object(HISReader, "get_connection", side_effect=_forbid_db), \
         patch.object(_socket_mod, "create_connection", side_effect=_forbid_db):
        yield conn_calls
    assert len(conn_calls) == 0, f"期望零连接尝试，实际 {len(conn_calls)}"


try:
    import pytest

    @pytest.fixture(autouse=True)
    def _pytest_offline_guard():
        with offline_connection_guard():
            yield
except ImportError:
    pass


if __name__ == "__main__":
    print("=" * 70)
    print(">>> 开始执行全链路离线鉴权与 HIS 读取器单元测试 <<<")
    print("=" * 70)
    with offline_connection_guard():
        test_his_fixture_removed()
        test_auth_scope_001_doctor_precedence()
        test_auth_scope_002_and_snap_query_001()
    print("=" * 70)
    print(">>> 全部离线鉴权与 HIS 读取器测试 100% 通过！（真实 DB/网络调用次数 = 0） <<<")
    print("=" * 70)

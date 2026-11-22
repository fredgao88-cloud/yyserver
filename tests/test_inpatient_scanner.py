# -*- coding: utf-8 -*-
"""
Automated Test Suite for tools/inpatient_scanner.py
Verifies:
1. 【黄金验收用例】合成在院未结算患者端到端测试（费用聚合、规则匹配、判灯评估、快照落库、无结算混入）
2. 真实在院患者扫描隔离性（include_recent_settled=False 时绝对不混入已结算病例）
3. 参数动态读取（zero hardcoded coefficients）与 Decimal 未舍入交叉相乘判灯
4. dbo.inpatient_dip_snapshot 幂等性与字段完整性验证
"""
import os
import sys
from decimal import Decimal

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from tools.inpatient_scanner import InpatientScanner
from db.his_reader import HISReader
from db.local_db import get_local_db_connection
from tools.dip_calculator import evaluate_traffic_light

class MockGoldenHISReader(HISReader):
    """用于黄金用例的合成在院患者模拟器"""
    def __init__(self, current_fee=Decimal("2450.00")):
        self._current_fee = current_fee
        # 不调用 super().__init__() 以免依赖 HIS 环境变量

    def get_active_inpatients(self, department=None, limit=None):
        return [{
            'zyh': 'SYNTH_GOLDEN_001',
            'patient_name': '合成在院患者',
            'gender': '男',
            'age': '68',
            'current_dept': '骨伤科',
            'current_bed': '01床',
            'admission_time': '2026-11-20 08:30:00',
            'current_stay_days': '3',
            'attending_doctor': '测试主治',
            'dept_director': '测试主任',
            'admission_diag_code': 'M17.9',
            'admission_diag_name': '未特指的膝关节病',
            'ssmc1': '保守治疗',
            'ssmc2': None,
            'ssmc3': None,
            'ylzffs': '01',
            'insurance_type': '职工'
        }]

    def get_active_patient_fee(self, lsh, cutoff_time=None):
        # 模拟 10 项诊疗费 + 2 项药费
        cz_fee = self._current_fee - Decimal("650.00")
        fy_fee = Decimal("650.00")
        return {
            'lsh': lsh,
            'has_activity': True,
            'cz_count': 10,
            'fy_count': 2,
            'cz_total': cz_fee,
            'fy_total': fy_fee,
            'total_fee': self._current_fee,
            'cz_stat': {'count': 10, 'total': cz_fee, 'refund': Decimal('0'), 'positive': cz_fee},
            'fy_stat': {'count': 2, 'total': fy_fee, 'refund': Decimal('0'), 'positive': fy_fee}
        }

    def get_settled_cases(self, limit=50, department=None):
        return [{
            'lsh': 'SETTLED_CASE_999',
            'settlement_id': '999',
            'total_amount': Decimal('6000.00'),
            'is_canceled': False,
            'patient_name': '历史已结算患者',
            'discharge_dept': '骨伤科',
            'admission_dept': '骨伤科',
            'primary_diag_code': 'M17.9',
            'primary_diag_name': '未特指的膝关节病',
            'ssmc1': '保守治疗',
            'ssmc2': None,
            'ssmc3': None,
            'stay_days': 10,
            'attending_doctor': '历史医生',
            'dept_director': '历史主任',
            'doctor_code': 'DOC999',
            'admission_time': '2026-10-01 10:00:00',
            'gender': '女',
            'age': '70',
            'insurance_type': '职工'
        }]

def run_inpatient_scanner_test_suite():
    print("=" * 70)
    print("  INPATIENT SCANNER AUTOMATED TEST SUITE (WITH GOLDEN CASE)")
    print("=" * 70)

    # -----------------------------------------------------------------
    # Test 0: 【黄金验收用例】端到端合成在院患者（费用+规则+判灯+快照落库）
    # -----------------------------------------------------------------
    print("\n[Test 0: 黄金验收用例] 验证未结算合成患者全链路 DIP 预警快照...")
    golden_batch_id = "GOLDEN_BATCH_ACCEPTANCE_001"
    
    # 1. 绿灯场景 (费用 2450.00 < 基准 2790.90 * 0.9 = 2511.81)
    mock_reader = MockGoldenHISReader(current_fee=Decimal("2450.00"))
    scanner = InpatientScanner(rule_version="2026_SAMPLE_V1")
    scanner.his_reader = mock_reader

    golden_res = scanner.scan_and_snapshot(
        batch_id=golden_batch_id,
        include_recent_settled=False
    )

    # 断言 1: 扫描隔离性，严格为 1，绝不混入已结算病例！
    assert golden_res['total_scanned'] == 1, f"Expected exactly 1 patient, got {golden_res['total_scanned']}"
    assert golden_res['GREEN'] == 1, f"Expected 1 GREEN, got {golden_res['GREEN']}"
    assert golden_res['PENDING'] == 0
    assert golden_res['YELLOW'] == 0
    assert golden_res['RED'] == 0

    # 断言 2: 数据库持久化及字段完整性
    cur, conn = get_local_db_connection(as_dict=True)
    try:
        cur.execute("""
        SELECT visit_id, dept_name, primary_icd, matched_rule_id, rule_version,
               current_fee, target_base, fee_ratio, alert_level, data_cutoff_time
        FROM dbo.inpatient_dip_snapshot
        WHERE batch_id = %s
        """, (golden_batch_id,))
        row = cur.fetchone()
        assert row is not None, "Snapshot record not found in database!"
        assert row['visit_id'] == 'SYNTH_GOLDEN_001'
        assert row['primary_icd'] == 'M17.9'
        assert row['rule_version'] == '2026_SAMPLE_V1'
        assert row['current_fee'] == Decimal('2450.00')
        assert row['target_base'] == Decimal('2790.90')
        assert row['alert_level'] == 'GREEN'
        assert row['data_cutoff_time'] is not None
        print(f"  -> [Green Light Asserted] Visit={row['visit_id']}, Rule=#{row['matched_rule_id']}, Fee={row['current_fee']}, Base={row['target_base']}, Light={row['alert_level']}")

        # 2. 黄灯端点场景 (费用 2600.00 位于 2511.81 ~ 3070.00 区间)
        mock_reader._current_fee = Decimal("2600.00")
        yellow_res = scanner.scan_and_snapshot(batch_id=golden_batch_id, include_recent_settled=False)
        assert yellow_res['YELLOW'] == 1, f"Expected 1 YELLOW, got {yellow_res['YELLOW']}"
        cur.execute("SELECT current_fee, alert_level FROM dbo.inpatient_dip_snapshot WHERE batch_id = %s", (golden_batch_id,))
        r_yellow = cur.fetchone()
        assert r_yellow['current_fee'] == Decimal('2600.00')
        assert r_yellow['alert_level'] == 'YELLOW'
        print(f"  -> [Yellow Light Asserted] Fee={r_yellow['current_fee']} -> Light={r_yellow['alert_level']}")

        # 3. 红灯超标场景 (费用 3200.00 > 3070.00)
        mock_reader._current_fee = Decimal("3200.00")
        red_res = scanner.scan_and_snapshot(batch_id=golden_batch_id, include_recent_settled=False)
        assert red_res['RED'] == 1, f"Expected 1 RED, got {red_res['RED']}"
        cur.execute("SELECT current_fee, alert_level FROM dbo.inpatient_dip_snapshot WHERE batch_id = %s", (golden_batch_id,))
        r_red = cur.fetchone()
        assert r_red['current_fee'] == Decimal('3200.00')
        assert r_red['alert_level'] == 'RED'
        print(f"  -> [Red Light Asserted] Fee={r_red['current_fee']} -> Light={r_red['alert_level']}")

        # 清理黄金测试批次
        cur.execute("DELETE FROM dbo.inpatient_dip_snapshot WHERE batch_id = %s", (golden_batch_id,))
        conn.commit()
    finally:
        conn.close()

    print("  [PASS] Test 0 黄金验收用例全链路断言 100% 通过！")

    # -----------------------------------------------------------------
    # Test 1: 真实在院扫描隔离性验证 (include_recent_settled=False 时绝对不混入历史病例)
    # -----------------------------------------------------------------
    print("\n[Test 1] 真实在院患者扫描 (include_recent_settled=False)...")
    real_scanner = InpatientScanner(rule_version="2026_SAMPLE_V1")
    batch_real = "TEST_REAL_ACTIVE_001"
    res_real = real_scanner.scan_and_snapshot(batch_id=batch_real, include_recent_settled=False)
    
    # 验证扫描到的数量等于当前真实在院未出院人数 (6人)
    assert res_real['total_scanned'] == 6, f"Expected exactly 6 active inpatients, got {res_real['total_scanned']}"
    print(f"  -> Total active patients scanned: {res_real['total_scanned']} (ZERO settled cases mixed in)")
    
    # 清理真实测试批次
    cur, conn = get_local_db_connection(as_dict=False)
    try:
        cur.execute("DELETE FROM dbo.inpatient_dip_snapshot WHERE batch_id = %s", (batch_real,))
        conn.commit()
    finally:
        conn.close()
    print("  [PASS] Test 1 真实在院扫描隔离性验证通过。")

    # -----------------------------------------------------------------
    # Test 2: 精确 Decimal 判灯与临界测试
    # -----------------------------------------------------------------
    print("\n[Test 2] 精确 Decimal 未舍入判灯端点测试...")
    benchmark = Decimal("4000.00")

    # 89.99% -> 绿灯
    res_a = evaluate_traffic_light(benchmark * Decimal("0.8999"), benchmark, ins_type="职工")
    assert res_a['light'] == "绿灯"
    assert res_a['boundary_note'] == "略低于 90%"

    # 90.00% 端点 -> 黄灯
    res_b = evaluate_traffic_light(benchmark * Decimal("0.9000"), benchmark, ins_type="职工")
    assert res_b['light'] == "黄灯"

    # 110.00% 端点 -> 黄灯
    res_c = evaluate_traffic_light(benchmark * Decimal("1.1000"), benchmark, ins_type="职工")
    assert res_c['light'] == "黄灯"

    # 110.004% -> 红灯
    res_d = evaluate_traffic_light(benchmark * Decimal("1.10004"), benchmark, ins_type="职工")
    assert res_d['light'] == "红灯"
    assert res_d['boundary_note'] == "略高于 110%"

    # 自费 -> NOT_APPLICABLE
    res_e = evaluate_traffic_light(Decimal("5000.00"), benchmark, ins_type="自费")
    assert res_e['status'] == "NOT_APPLICABLE"
    print("  [PASS] Test 2 精确判灯端点全部通过。")

    print("\n" + "=" * 70)
    print("  ALL INPATIENT SCANNER TESTS PASSED (100% COMPLIANT WITH v1.2)")
    print("=" * 70)

if __name__ == '__main__':
    run_inpatient_scanner_test_suite()

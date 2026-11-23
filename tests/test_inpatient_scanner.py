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
    expected_active = len(real_scanner.his_reader.get_active_inpatients())
    res_real = real_scanner.scan_and_snapshot(batch_id=batch_real, include_recent_settled=False)
    
    # 验证扫描到的数量严格等于当前真实在院未出院人数，绝不混入结算病例
    assert res_real['total_scanned'] == expected_active, f"Expected exactly {expected_active} active inpatients, got {res_real['total_scanned']}"
    assert res_real['total_scanned'] > 0, "Active inpatients should be greater than 0"
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

    # -----------------------------------------------------------------
    # Test 3: 零基准容错测试 (单批次包含零基准病例与正常病例，两者均成功入库，绝不崩溃)
    # -----------------------------------------------------------------
    print("\n[Test 3] 验证零基准防崩机制 (同一批次第1条零基准、第2条正常，两者均成功写入快照)...")
    import tools.inpatient_scanner as scanner_module
    orig_match = scanner_module.match_dip_rule

    zero_batch_id = "BATCH_ZERO_BENCHMARK_TEST_001"
    cases_test3 = [
        {
            'zyh': 'SYNTH_ZERO_BENCH_01',
            'patient_name': '零基准患者',
            'gender': '男',
            'age': '68',
            'current_dept': '骨伤科',
            'current_bed': '02床',
            'admission_time': '2026-11-20 08:30:00',
            'current_stay_days': '3',
            'attending_doctor': '测试医生',
            'dept_director': '测试主任',
            'admission_diag_code': 'M17.9',
            'admission_diag_name': '未特指的膝关节病',
            'ssmc1': '保守治疗',
            'ylzffs': '01',
            'insurance_type': '职工'
        },
        {
            'zyh': 'SYNTH_NORMAL_CASE_02',
            'patient_name': '正常患者',
            'gender': '女',
            'age': '68',
            'current_dept': '骨伤科',
            'current_bed': '03床',
            'admission_time': '2026-11-20 08:30:00',
            'current_stay_days': '3',
            'attending_doctor': '测试医生',
            'dept_director': '测试主任',
            'admission_diag_code': 'M17.9',
            'admission_diag_name': '未特指的膝关节病',
            'ssmc1': '保守治疗',
            'ylzffs': '01',
            'insurance_type': '职工'
        }
    ]

    class MockMultiCaseHISReader(HISReader):
        def __init__(self, cases):
            self._cases = cases
        def get_active_inpatients(self, department=None, limit=None):
            return self._cases
        def get_active_patient_fee(self, lsh, cutoff_time=None):
            return {
                'lsh': lsh,
                'has_activity': True,
                'cz_count': 5,
                'fy_count': 1,
                'cz_total': Decimal('2000.00'),
                'fy_total': Decimal('450.00'),
                'total_fee': Decimal('2450.00'),
                'cz_stat': {'count': 5, 'total': Decimal('2000.00'), 'refund': Decimal('0'), 'positive': Decimal('2000.00')},
                'fy_stat': {'count': 1, 'total': Decimal('450.00'), 'refund': Decimal('0'), 'positive': Decimal('450.00')}
            }
        def get_settled_cases(self, limit=50, department=None):
            return []

    mock_r3 = MockMultiCaseHISReader(cases_test3)
    s3 = InpatientScanner(rule_version="2026_SAMPLE_V1")
    s3.his_reader = mock_r3

    call_tracker = {'count': 0}
    def mock_match_dip(disease, **kwargs):
        call_tracker['count'] += 1
        res = orig_match(disease, **kwargs)
        if call_tracker['count'] == 1:
            res_zero = dict(res)
            res_zero["benchmark_amount"] = 0.0
            return res_zero
        return res

    scanner_module.match_dip_rule = mock_match_dip
    try:
        res_batch3 = s3.scan_and_snapshot(batch_id=zero_batch_id, include_recent_settled=False)
        assert res_batch3['total_scanned'] == 2, f"Expected 2 scanned, got {res_batch3['total_scanned']}"
        assert res_batch3['PENDING'] == 1, f"Expected 1 PENDING, got {res_batch3['PENDING']}"
        assert res_batch3['GREEN'] == 1, f"Expected 1 GREEN, got {res_batch3['GREEN']}"

        cur, conn = get_local_db_connection(as_dict=True)
        try:
            cur.execute("""
            SELECT visit_id, target_base, fee_ratio, alert_level, pending_reason
            FROM dbo.inpatient_dip_snapshot
            WHERE batch_id = %s
            ORDER BY visit_id
            """, (zero_batch_id,))
            rows = cur.fetchall()
            assert len(rows) == 2, f"Expected 2 rows in DB snapshot, got {len(rows)}"
            
            normal_row = [r for r in rows if r['visit_id'] == 'SYNTH_NORMAL_CASE_02'][0]
            assert normal_row['alert_level'] == 'GREEN'
            assert normal_row['target_base'] == Decimal('2790.90')
            assert normal_row['fee_ratio'] is not None

            zero_row = [r for r in rows if r['visit_id'] == 'SYNTH_ZERO_BENCH_01'][0]
            assert zero_row['alert_level'] == 'PENDING'
            assert zero_row['pending_reason'] == 'INVALID_BENCHMARK'
            assert zero_row['fee_ratio'] is None
            print("  -> Zero-benchmark case successfully marked PENDING (INVALID_BENCHMARK) without DivisionByZero crash.")
            print("  -> Both cases successfully persisted to dbo.inpatient_dip_snapshot.")

            cur.execute("DELETE FROM dbo.inpatient_dip_snapshot WHERE batch_id = %s", (zero_batch_id,))
            conn.commit()
        finally:
            conn.close()
    finally:
        scanner_module.match_dip_rule = orig_match
    print("  [PASS] Test 3 零基准防崩与批次双用例完整落库测试通过！")

    # -----------------------------------------------------------------
    # Test 4: 公费医疗等未支持参保类型合规拦截测试
    # -----------------------------------------------------------------
    print("\n[Test 4] 验证公费医疗拦截 (严禁套用居民标准，统一标记 PENDING)...")
    gongfei_batch_id = "BATCH_GONGFEI_TEST_001"
    cases_test4 = [{
        'zyh': 'SYNTH_GONGFEI_01',
        'patient_name': '公费医疗患者',
        'gender': '男',
        'age': '68',
        'current_dept': '骨伤科',
        'current_bed': '05床',
        'admission_time': '2026-11-20 08:30:00',
        'current_stay_days': '3',
        'attending_doctor': '测试医生',
        'dept_director': '测试主任',
        'admission_diag_code': 'M17.9',
        'admission_diag_name': '未特指的膝关节病',
        'ssmc1': '保守治疗',
        'ylzffs': '3',
        'insurance_type': '公费医疗'
    }]
    mock_r4 = MockMultiCaseHISReader(cases_test4)
    s4 = InpatientScanner(rule_version="2026_SAMPLE_V1")
    s4.his_reader = mock_r4

    res_batch4 = s4.scan_and_snapshot(batch_id=gongfei_batch_id, include_recent_settled=False)
    assert res_batch4['total_scanned'] == 1
    assert res_batch4['PENDING'] == 1, f"Expected 1 PENDING, got {res_batch4}"
    
    cur, conn = get_local_db_connection(as_dict=True)
    try:
        cur.execute("""
        SELECT visit_id, ins_type, target_base, alert_level, pending_reason
        FROM dbo.inpatient_dip_snapshot
        WHERE batch_id = %s
        """, (gongfei_batch_id,))
        r_gf = cur.fetchone()
        assert r_gf is not None
        assert r_gf['ins_type'] == '公费医疗'
        assert r_gf['alert_level'] == 'PENDING'
        assert 'UNSUPPORTED_INS_TYPE' in r_gf['pending_reason']
        print(f"  -> 公费医疗患者: ins_type={r_gf['ins_type']}, alert_level={r_gf['alert_level']}, reason={r_gf['pending_reason']}")

        cur.execute("DELETE FROM dbo.inpatient_dip_snapshot WHERE batch_id = %s", (gongfei_batch_id,))
        conn.commit()
    finally:
        conn.close()
    print("  [PASS] Test 4 公费医疗合规拦截测试通过！")

    # -----------------------------------------------------------------
    # Test 5: 城镇居民医保判灯一致性测试（绝不因“城镇”误判为职工绿灯）
    # -----------------------------------------------------------------
    print("\n[Test 5] 验证城镇居民医保判定 (费用2450元 -> 居民基准2604.84元 -> 黄灯)...")
    city_res_batch_id = "BATCH_CITY_RESIDENT_001"
    cases_test5 = [{
        'zyh': 'SYNTH_CITY_RES_01',
        'patient_name': '城镇居民患者',
        'gender': '女',
        'age': '68',
        'current_dept': '骨伤科',
        'current_bed': '06床',
        'admission_time': '2026-11-20 08:30:00',
        'current_stay_days': '3',
        'attending_doctor': '测试医生',
        'dept_director': '测试主任',
        'admission_diag_code': 'M17.9',
        'admission_diag_name': '未特指的膝关节病',
        'ssmc1': '保守治疗',
        'ylzffs': '2',
        'insurance_type': '城镇居民医保'
    }]
    mock_r5 = MockMultiCaseHISReader(cases_test5)
    s5 = InpatientScanner(rule_version="2026_SAMPLE_V1")
    s5.his_reader = mock_r5

    res_batch5 = s5.scan_and_snapshot(batch_id=city_res_batch_id, include_recent_settled=False)
    assert res_batch5['total_scanned'] == 1
    assert res_batch5['YELLOW'] == 1, f"Expected 1 YELLOW, got {res_batch5}"

    cur, conn = get_local_db_connection(as_dict=True)
    try:
        cur.execute("""
        SELECT visit_id, ins_type, target_base, alert_level
        FROM dbo.inpatient_dip_snapshot
        WHERE batch_id = %s
        """, (city_res_batch_id,))
        r_cr = cur.fetchone()
        assert r_cr is not None
        assert r_cr['ins_type'] == '居民'
        assert r_cr['target_base'] == Decimal('2604.84')
        assert r_cr['alert_level'] == 'YELLOW'
        print(f"  -> 城镇居民医保患者: ins_type={r_cr['ins_type']}, base={r_cr['target_base']}, light={r_cr['alert_level']} (黄灯验收通过)")

        cur.execute("DELETE FROM dbo.inpatient_dip_snapshot WHERE batch_id = %s", (city_res_batch_id,))
        conn.commit()
    finally:
        conn.close()
    print("  [PASS] Test 5 城镇居民医保黄灯判定验证通过！")

    print("\n" + "=" * 70)
    print("  ALL INPATIENT SCANNER TESTS PASSED (100% COMPLIANT WITH v1.2)")
    print("=" * 70)

if __name__ == '__main__':
    run_inpatient_scanner_test_suite()

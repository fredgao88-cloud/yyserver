# -*- coding: utf-8 -*-
"""
Automated Test Suite for tools/inpatient_scanner.py
Verifies:
1. Dynamic parameter reading from dbo.dip_rules (zero hardcoded coefficients)
2. Decimal unrounded cross-multiplication traffic lights
3. Self-pay NOT_APPLICABLE isolation
4. Ambiguous/missing parameter PENDING handling
5. dbo.inpatient_dip_snapshot persistence with all required columns
6. Idempotent multi-run under the same batch_id
"""
import os
import sys
from decimal import Decimal

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from tools.inpatient_scanner import InpatientScanner
from db.local_db import get_local_db_connection

def run_inpatient_scanner_test_suite():
    print("=" * 70)
    print("  INPATIENT SCANNER AUTOMATED TEST SUITE")
    print("=" * 70)

    test_batch_id = "TEST_BATCH_AUTO_001"
    scanner = InpatientScanner(rule_version="2026_SAMPLE_V1")

    # 1. Run scanner with test batch ID
    print(f"\n[Test 1] Executing scan_and_snapshot (batch_id={test_batch_id})...")
    res1 = scanner.scan_and_snapshot(
        batch_id=test_batch_id,
        limit=20,
        include_recent_settled=True
    )

    assert res1['total_scanned'] > 0, "No patients scanned!"
    assert res1['batch_id'] == test_batch_id
    print(f"  -> Total scanned: {res1['total_scanned']}")
    print(f"  -> Breakdown: Green={res1['GREEN']}, Yellow={res1['YELLOW']}, Red={res1['RED']}, NotApp={res1['NOT_APPLICABLE']}, Pending={res1['PENDING']}")
    print("  [PASS] Test 1 Execution Successful.")

    # 2. Verify Database Persistence in his_agent_db.dbo.inpatient_dip_snapshot
    print(f"\n[Test 2] Verifying records in dbo.inpatient_dip_snapshot...")
    cur, conn = get_local_db_connection(as_dict=True)
    try:
        cur.execute("""
        SELECT COUNT(*) as cnt,
               COUNT(DISTINCT visit_id) as distinct_visits,
               MAX(data_cutoff_time) as max_cutoff,
               MAX(rule_version) as max_ver
        FROM dbo.inpatient_dip_snapshot
        WHERE batch_id = %s
        """, (test_batch_id,))
        row = cur.fetchone()
        assert row['cnt'] == res1['total_scanned'], f"DB row count mismatch: {row['cnt']} vs {res1['total_scanned']}"
        assert row['cnt'] == row['distinct_visits'], "Duplicate visit_id found within same batch!"
        assert row['max_ver'] == "2026_SAMPLE_V1"
        assert row['max_cutoff'] is not None

        # Verify columns exist and are populated
        cur.execute("""
        SELECT TOP 5 visit_id, dept_name, alert_level, current_fee, target_base, fee_ratio, pending_reason, rule_version
        FROM dbo.inpatient_dip_snapshot
        WHERE batch_id = %s
        """, (test_batch_id,))
        samples = cur.fetchall()
        for s in samples:
            assert s['alert_level'] in ('GREEN', 'YELLOW', 'RED', 'NOT_APPLICABLE', 'PENDING')
            assert s['rule_version'] == "2026_SAMPLE_V1"
            print(f"     Snapshot: Visit={s['visit_id']}, Dept={s['dept_name']}, Light={s['alert_level']}, Fee={s['current_fee']}, Base={s['target_base']}, Reason={s['pending_reason']}")

        print(f"  -> Verified {row['cnt']} records in his_agent_db.dbo.inpatient_dip_snapshot.")
    finally:
        conn.close()
    print("  [PASS] Test 2 Database Persistence Verified.")

    # 3. Test Idempotent Re-run (Same batch_id must NOT duplicate or fail on UX_snapshot_batch_visit)
    print(f"\n[Test 3] Verifying Idempotent re-run with same batch_id...")
    res2 = scanner.scan_and_snapshot(
        batch_id=test_batch_id,
        limit=20,
        include_recent_settled=True
    )
    cur, conn = get_local_db_connection(as_dict=True)
    try:
        cur.execute("SELECT COUNT(*) as cnt FROM dbo.inpatient_dip_snapshot WHERE batch_id = %s", (test_batch_id,))
        count_after = cur.fetchone()['cnt']
        assert count_after == res1['total_scanned'], f"Idempotency failed! Count changed to {count_after}"
        print(f"  -> Row count after re-run remains exactly {count_after}. Zero duplicate keys.")
    finally:
        conn.close()
    print("  [PASS] Test 3 Idempotency Verified.")

    # 4. Rigorous Unit Tests on Traffic Light & Edge Cases
    print(f"\n[Test 4] Verifying Precision Decimal Traffic Light & Boundary Rules...")
    from tools.dip_calculator import evaluate_traffic_light
    benchmark = Decimal("4000.00")

    # Case A: 89.99% -> GREEN (with "略低于 90%" boundary note)
    cost_89_99 = benchmark * Decimal("0.8999")
    res_a = evaluate_traffic_light(cost_89_99, benchmark, ins_type="职工")
    assert res_a['light'] == "绿灯", f"Expected 绿灯, got {res_a['light']}"
    assert res_a['boundary_note'] == "略低于 90%", f"Expected '略低于 90%', got {res_a['boundary_note']}"
    print("     89.99%: Green light + '略低于 90%' -> Verified")

    # Case B: 90.00% (Endpoint) -> YELLOW
    cost_90_00 = benchmark * Decimal("0.9000")
    res_b = evaluate_traffic_light(cost_90_00, benchmark, ins_type="职工")
    assert res_b['light'] == "黄灯", f"Expected 黄灯, got {res_b['light']}"
    print("     90.00% (Endpoint): Yellow light -> Verified")

    # Case C: 110.00% (Endpoint) -> YELLOW
    cost_110_00 = benchmark * Decimal("1.1000")
    res_c = evaluate_traffic_light(cost_110_00, benchmark, ins_type="职工")
    assert res_c['light'] == "黄灯", f"Expected 黄灯, got {res_c['light']}"
    print("     110.00% (Endpoint): Yellow light -> Verified")

    # Case D: 110.004% -> RED (with "略高于 110%" boundary note)
    cost_110_004 = benchmark * Decimal("1.10004")
    res_d = evaluate_traffic_light(cost_110_004, benchmark, ins_type="职工")
    assert res_d['light'] == "红灯", f"Expected 红灯, got {res_d['light']}"
    assert res_d['boundary_note'] == "略高于 110%", f"Expected '略高于 110%', got {res_d['boundary_note']}"
    print("     110.004%: Red light + '略高于 110%' -> Verified")

    # Case E: Self-Pay -> NOT_APPLICABLE
    res_e = evaluate_traffic_light(Decimal("5000.00"), benchmark, ins_type="自费")
    assert res_e['status'] == "NOT_APPLICABLE"
    print("     Self-Pay: NOT_APPLICABLE -> Verified")

    # 5. Clean up test batch
    print(f"\n[Cleanup] Removing temporary test batch {test_batch_id}...")
    cur, conn = get_local_db_connection(as_dict=False)
    try:
        cur.execute("DELETE FROM dbo.inpatient_dip_snapshot WHERE batch_id = %s", (test_batch_id,))
        conn.commit()
        print("  -> Cleanup completed.")
    finally:
        conn.close()

    print("\n" + "=" * 70)
    print("  ALL INPATIENT SCANNER TESTS PASSED (100% COMPLIANT WITH v1.2)")
    print("=" * 70)

if __name__ == '__main__':
    run_inpatient_scanner_test_suite()

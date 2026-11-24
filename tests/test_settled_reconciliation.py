# -*- coding: utf-8 -*-
"""
Automated Test Suite: Settled Case Historical Replay & Reconciliation
(已结算历史病例回放对账自动化测试套件)

Verifies:
1. Table Association & Primary Keys: his_zyjz(lsh, id), his_zycjz(lsh, id), his_zycz(lsh), his_bl_sy(lsh), his_ZY_CYDJXX(ZYH)
2. Settlement Reversal & Cancellation: iszf = 1 vs iszf = 0 logic
3. Itemized Refund / Reversal Deductions: negative items in his_zycz and his_cfmain_fyjl properly reduce totals
4. Accounting Formula Verification:
   - his_zyjz.amoney == sum(his_zycjz.amoney) (100% exact match across all cases)
   - his_zyjz.czmoney + his_zyjz.yjmoney == sum(his_zycz.money) (exact in 48/50, 2 cases with category 30 other fee)
   - his_zyjz.yfmoney ~= sum(his_cfmain_fyjl.bmoney) + 已计费未发药处方(his_cfmain isfy=0 且未作废) (within rounding tolerance)
5. Ward & Bed Transfer Tracking: his_zk transition consistency
6. Batch Idempotency: Multiple reconciliation runs yield identical deterministic outcomes
"""
import os
import sys
from decimal import Decimal, ROUND_HALF_UP

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from db.his_reader import HISReader

def run_settled_reconciliation_suite():
    print("=" * 70)
    print("  SETTLED INPATIENT CASE HISTORICAL REPLAY & RECONCILIATION SUITE")
    print("=" * 70)

    reader = HISReader()

    skipped_suites = []  # 未执行的套件/轮次必须计入 SKIP，结尾横幅如实报告（RECON-RX-001 验收要求）

    # -------------------------------------------------------------
    # Test Suite 1: Query Settled Cases & Key Associations
    # -------------------------------------------------------------
    print("\n[Suite 1] Querying 50 settled cases and verifying key associations...")
    settled_cases = reader.get_settled_cases(limit=50)
    assert len(settled_cases) > 0, "No settled cases returned!"
    print(f"  -> Successfully retrieved {len(settled_cases)} active settled cases.")

    valid_keys = 0
    matched_sy = 0
    matched_cydj = 0
    for case in settled_cases:
        assert case['lsh'] is not None and len(case['lsh']) > 0, "Missing lsh in his_zyjz!"
        assert case['settlement_id'] is not None, "Missing id in his_zyjz!"
        assert case['total_amount'] > 0, "Settlement amount must be positive!"
        assert case['is_canceled'] == False, "Canceled settlement (iszf=1) found in active query!"
        valid_keys += 1
        if case['patient_name']:
            matched_sy += 1
        if case['primary_diag_code']:
            matched_cydj += 1

    print(f"  -> Valid settlement keys: {valid_keys}/{len(settled_cases)}")
    print(f"  -> Successfully mapped to his_bl_sy (front page): {matched_sy}/{len(settled_cases)}")
    print(f"  -> Successfully mapped to his_ZY_CYDJXX (ICD-10 codes): {matched_cydj}/{len(settled_cases)}")
    print("  [PASS] Suite 1 Key Association Verified.")

    # -------------------------------------------------------------
    # Test Suite 2: Accounting Formula & Category Ledger Reconciliation
    # -------------------------------------------------------------
    print("\n[Suite 2] Reconciling accounting formulas across settled admissions (deduplicated by lsh)...")
    exact_category_matches = 0
    exact_clinical_matches = 0
    close_pharmacy_matches = 0

    # 按就诊流水号去重，确保每次住院按整次对账（杜绝多结算单拆分造成的范围失配）
    unique_cases = {}
    for case in settled_cases:
        if case['lsh'] not in unique_cases:
            unique_cases[case['lsh']] = case
    admission_cases = list(unique_cases.values())
    print(f"  -> Total distinct settled admissions for reconciliation: {len(admission_cases)}")

    reconciled_details = []

    for case in admission_cases:
        lsh = case['lsh']
        detail = reader.get_case_fee_detail(lsh)
        assert detail['reconciliation_scope'] in ('SINGLE_SETTLEMENT', 'ADMISSION_LEVEL')
        
        settled_master = detail['zyjz']
        total_amount = Decimal(str(settled_master['amoney']))
        czmoney = Decimal(str(settled_master.get('czmoney') or 0))
        yjmoney = Decimal(str(settled_master.get('yjmoney') or 0))
        yfmoney = Decimal(str(settled_master.get('yfmoney') or 0))

        cat_sum = detail['category_sum']
        cz_total = detail['cz_stat']['total']
        fy_total = detail['fy_stat']['total']

        # Check Category Ledger (his_zycjz vs his_zyjz master)
        diff_cat = abs(cat_sum - total_amount)
        if diff_cat < Decimal('0.01'):
            exact_category_matches += 1

        # Check Clinical Items (his_zycz vs czmoney + yjmoney)
        diff_cz = abs(cz_total - (czmoney + yjmoney))
        if diff_cz < Decimal('0.06'):
            exact_clinical_matches += 1

        # Check Pharmacy Dispensing (his_cfmain_fyjl vs yfmoney)
        # 口径：发药明细合计 + 已计费未发药处方（出院当天结算常先于药房发药，属正常时序差，
        # 由 his_reader 单列 undispensed 统计）≈ 结算主单 yfmoney；放松该时序差但不掩盖真实漏记
        fy_undispensed = Decimal(str(detail['fy_stat'].get('undispensed', 0)))
        diff_fy = abs(fy_total + fy_undispensed - yfmoney)
        if diff_fy <= Decimal('0.10'):
            close_pharmacy_matches += 1

        reconciled_details.append({
            'lsh': lsh,
            'total': total_amount,
            'cat_sum': cat_sum,
            'cz_total': cz_total,
            'cz_expected': czmoney + yjmoney,
            'fy_total': fy_total,
            'fy_expected': yfmoney
        })

    category_match_rate = exact_category_matches / len(admission_cases)
    clinical_match_rate = exact_clinical_matches / len(admission_cases)
    pharmacy_match_rate = close_pharmacy_matches / len(admission_cases)

    print(f"  -> Category Ledger (his_zycjz) Exact Match: {exact_category_matches}/{len(admission_cases)} ({category_match_rate:.1%})")
    print(f"  -> Clinical Item (his_zycz vs cz+yj) Match (<=0.05Y): {exact_clinical_matches}/{len(admission_cases)} ({clinical_match_rate:.1%})")
    print(f"  -> Pharmacy Dispensing Match (dispensed + charged-undispensed vs master, within 0.10Y): {close_pharmacy_matches}/{len(admission_cases)} ({pharmacy_match_rate:.1%})")
    assert category_match_rate == 1.0, f"Expected 100% category match, got {category_match_rate:.1%}"
    assert clinical_match_rate >= 0.90, f"Expected >=90.0% clinical matches, got {clinical_match_rate:.1%}"
    assert pharmacy_match_rate >= 0.95, f"Expected >=95.0% pharmacy matches, got {pharmacy_match_rate:.1%}"
    print("  [PASS] Suite 2 Accounting Formula Verified.")

    # -------------------------------------------------------------
    # Test Suite 3: Refund & Reversal Logic Verification
    # -------------------------------------------------------------
    print("\n[Suite 3] Verifying refund & negative item offset logic...")
    refund_verified_count = 0
    conn = reader.get_connection()
    cur = conn.cursor(as_dict=True)
    try:
        # Find settled patients with negative/refund items (money < 0 in his_zycz)
        cur.execute("""
        SELECT TOP 5 jz.lsh, jz.id, jz.amoney, COUNT(*) as refund_item_count, SUM(cz.money) as refund_amt
        FROM dbo.his_zyjz jz
        INNER JOIN dbo.his_zycz cz ON jz.lsh = cz.lsh
        WHERE jz.iszf = 0 AND cz.money < 0
        GROUP BY jz.lsh, jz.id, jz.amoney
        HAVING COUNT(*) > 0
        """)
        refund_cases = cur.fetchall()
        print(f"  -> Found {len(refund_cases)} settled cases with negative/refund items in his_zycz.")
        for rc in refund_cases:
            lsh = rc['lsh']
            masked_lsh = lsh[:4] + "****" if len(lsh) > 4 else "***"
            detail = reader.get_case_fee_detail(lsh, settlement_id=rc['id'])
            pos_amt = detail['cz_stat']['positive']
            ref_amt = detail['cz_stat']['refund']
            tot_amt = detail['cz_stat']['total']

            # Assert positive + refund == total (where refund is negative)
            assert ref_amt < Decimal('0'), f"Refund amount should be strictly negative, got {ref_amt}"
            assert abs((pos_amt + ref_amt) - tot_amt) < Decimal('0.01'), \
                f"Refund net math mismatch: {pos_amt} + {ref_amt} != {tot_amt}"
            refund_verified_count += 1
            print(f"     Patient {masked_lsh}: PositiveItems={pos_amt}, RefundOffset={ref_amt}, NetTotal={tot_amt} -> Verified!")

        assert refund_verified_count > 0, "No refund cases could be verified!"
        print(f"  -> Successfully verified {refund_verified_count} cases with refund deductions.")
    finally:
        conn.close()
    print("  [PASS] Suite 3 Refund Deductions Verified.")

    # -------------------------------------------------------------
    # Test Suite 4: Ward & Bed Transfer Tracking
    # -------------------------------------------------------------
    print("\n[Suite 4] Verifying ward/bed transfer tracking (his_zk)...")
    sample_lsh = 'zy2012092600001'
    transfers = reader.get_patient_transfers(sample_lsh)
    print(f"  -> Sample case transfers found: {len(transfers)}")
    for t in transfers:
        print(f"     Transfer ID={t['id']}, Date={t['zkrq']}")
        assert t['yksid'] is not None and t['xksid'] is not None
    print("  [PASS] Suite 4 Ward & Bed Transfers Verified.")

    # -------------------------------------------------------------
    # Test Suite 5: Batch Idempotency Verification
    # -------------------------------------------------------------
    print("\n[Suite 5] Verifying multi-run idempotency...")
    run1 = reader.get_settled_cases(limit=20)
    run2 = reader.get_settled_cases(limit=20)

    assert len(run1) == len(run2) == 20
    for i in range(20):
        assert run1[i]['lsh'] == run2[i]['lsh']
        assert run1[i]['settlement_id'] == run2[i]['settlement_id']
        assert run1[i]['total_amount'] == run2[i]['total_amount']
        assert run1[i]['settlement_time'] == run2[i]['settlement_time']

    print(f"  -> Ran 2 consecutive batches of 20 cases. Hash/data comparison: 100% identical.")
    print("  [PASS] Suite 5 Batch Idempotency Verified.")

    # -------------------------------------------------------------
    # Test Suite 6: Multi-settlement Reconciliation & Scope Labeling
    # -------------------------------------------------------------
    print("\n[Suite 6] Verifying multi-settlement reconciliation & scope granularity labeling...")
    # Verify scope labeling on real settled cases
    for case in settled_cases[:10]:
        lsh = case['lsh']
        detail = reader.get_case_fee_detail(lsh)
        assert detail.get('reconciliation_scope') in ('SINGLE_SETTLEMENT', 'ADMISSION_LEVEL')
        assert detail.get('settlement_count') >= 1

    # Synthetic Multi-settlement Case: "一次住院、两次结算、一次退费"
    # Verification using the actual get_case_fee_detail implementation:
    # 验证无论是否传入 settlement_id，多次结算就诊均自动升级为 ADMISSION_LEVEL，对齐主账与明细
    class MockMultiSettlementReader(HISReader):
        def get_connection(self):
            class MockCursor:
                def __init__(self):
                    self._query = ""
                def execute(self, sql, params=()):
                    self._query = sql
                def fetchall(self):
                    if "FROM dbo.his_zyjz" in self._query:
                        return [
                            {'id': 101, 'lsh': 'zy_multi_synth', 'amoney': Decimal('1200.00'), 'czmoney': Decimal('800.00'), 'yjmoney': Decimal('0.00'), 'yfmoney': Decimal('400.00'), 'iszf': 0},
                            {'id': 102, 'lsh': 'zy_multi_synth', 'amoney': Decimal('800.00'), 'czmoney': Decimal('500.00'), 'yjmoney': Decimal('0.00'), 'yfmoney': Decimal('300.00'), 'iszf': 0}
                        ]
                    elif "FROM dbo.his_zycjz" in self._query:
                        return [
                            {'xmsflxid': '01', 'amoney': Decimal('1300.00'), 'ssmoney': Decimal('1300.00')},
                            {'xmsflxid': '02', 'amoney': Decimal('700.00'), 'ssmoney': Decimal('700.00')}
                        ]
                    return []
                def fetchone(self):
                    if "unfy_count" in self._query:
                        # 合并语句（RECON-RX-001）：发药明细 + 未发药处方同一语句快照
                        # （合成用例无未发药处方）
                        return {'fy_count': 5, 'fy_total': Decimal('700.00'),
                                'fy_refund_total': Decimal('0.00'), 'fy_positive_total': Decimal('700.00'),
                                'unfy_count': 0, 'unfy_total': None}
                    elif "FROM dbo.his_zycz" in self._query:
                        return {'cz_count': 10, 'cz_total': Decimal('1300.00'), 'cz_refund_total': Decimal('-100.00'), 'cz_positive_total': Decimal('1400.00')}
                    return None
                def close(self):
                    pass
            class MockConn:
                def cursor(self, as_dict=True):
                    return MockCursor()
                def autocommit(self, value):
                    pass
                def commit(self):
                    pass
                def rollback(self):
                    pass
                def close(self):
                    pass
            return MockConn()

    multi_reader = MockMultiSettlementReader()
    # 调用路径 1: 不传 settlement_id，自动按整次住院汇总
    detail_no_id = multi_reader.get_case_fee_detail('zy_multi_synth')
    assert detail_no_id['reconciliation_scope'] == 'ADMISSION_LEVEL'
    assert detail_no_id['settlement_count'] == 2
    assert detail_no_id['category_sum'] == Decimal('2000.00')
    assert detail_no_id['zyjz']['amoney'] == Decimal('2000.00')
    assert detail_no_id['cz_stat']['total'] == Decimal('1300.00')
    assert detail_no_id['fy_stat']['total'] == Decimal('700.00')
    assert detail_no_id['fy_stat']['undispensed'] == Decimal('0.00')
    assert detail_no_id['fy_stat']['undispensed_count'] == 0

    # 调用路径 2: 逐单传入有效 settlement_id（仍保证对账范围与明细一致，自动升级 ADMISSION_LEVEL）
    detail_with_id = multi_reader.get_case_fee_detail('zy_multi_synth', settlement_id='101')
    assert detail_with_id['reconciliation_scope'] == 'ADMISSION_LEVEL'
    assert detail_with_id['settlement_count'] == 2
    assert detail_with_id['category_sum'] == Decimal('2000.00')
    assert detail_with_id['zyjz']['amoney'] == Decimal('2000.00')
    assert detail_with_id['cz_stat']['total'] == Decimal('1300.00')
    assert detail_with_id['fy_stat']['total'] == Decimal('700.00')

    # 调用路径 3: 传入不存在的结算单号 (999) -> 严格拦截，返回空字典 {}，绝不返回假阳性金额掩盖参数错误
    detail_non_existent = multi_reader.get_case_fee_detail('zy_multi_synth', settlement_id='999')
    assert detail_non_existent == {}, f"Expected empty dict for non-existent settlement_id 999, got {detail_non_existent}"

    # 调用路径 4: 传入已作废单号或属于其他住院记录的单号 -> 严格返回空字典 {}
    detail_voided = multi_reader.get_case_fee_detail('zy_multi_synth', settlement_id='888')
    assert detail_voided == {}, f"Expected empty dict for invalid settlement_id 888, got {detail_voided}"

    print("  -> 结算单参数校验核验通过：有效单号严格升级对齐，不存在单号(999)与无效单号严格返回空结果。")
    print("  [PASS] Suite 6 Multi-settlement Reconciliation & Scope Verified.")

    # -------------------------------------------------------------
    # Test Suite 7: 未发药处方口径独立样例（RECON-TEST-001 / RECON-RX-001）
    # 非零未发药、未计费、作废、已发药、真实差额五类样例分别断言笔数与金额，
    # 并断言发药+未发药两组聚合由同一条 SQL 语句完成（语句级一致快照，无读间漏算窗口）
    # -------------------------------------------------------------
    print("\n[Suite 7] Verifying undispensed-prescription scope with independent samples...")
    RX_CFMAIN = [
        # 已发药：fyjl 有对应记录，绝不计入未发药
        {'id': 'RX-DISP-0001', 'bmoney': Decimal('30.00'), 'jsrq': '2026-11-24 09:12:00', 'iszf': None},
        # 已计费未发药：计入未发药（合成编号，非真实就诊流水号）
        {'id': 'RX-UNFY-0001', 'bmoney': Decimal('12.00'), 'jsrq': '2026-11-24 09:30:00', 'iszf': None},
        # 未计费弃用处方（jsrq 为空）：不计入
        {'id': 'RX-UNBILL-01', 'bmoney': Decimal('99.00'), 'jsrq': None, 'iszf': None},
        # 作废处方：不计入
        {'id': 'RX-VOID-0001', 'bmoney': Decimal('55.00'), 'jsrq': '2026-11-24 09:40:00', 'iszf': 1},
    ]
    RX_FYJL = [
        {'id': 'RX-DISP-0001', 'bmoney': Decimal('30.00')},
        {'id': 'RX-DISP-0001', 'bmoney': Decimal('-5.00')},  # 退药对照：发药合计含负数冲减
    ]

    class RxMockCursor:
        def __init__(self):
            self._query = ""
            self.combined_query_calls = 0
        def execute(self, sql, params=()):
            self._query = sql
            if "unfy_count" in sql:
                self.combined_query_calls += 1
        def fetchall(self):
            if "FROM dbo.his_zyjz" in self._query:
                return [{'id': 201, 'lsh': 'zy_synth_rx', 'amoney': Decimal('200.00'),
                         'czmoney': Decimal('0.00'), 'yjmoney': Decimal('0.00'),
                         'yfmoney': Decimal('37.00'), 'iszf': 0}]
            if "FROM dbo.his_zycjz" in self._query:
                return [{'xmsflxid': '01', 'amoney': Decimal('200.00'), 'ssmoney': Decimal('200.00')}]
            return []
        def fetchone(self):
            if "unfy_count" in self._query:
                # 按读取器判据在内存中模拟同一条合并语句的语义
                fyjl_ids = {f['id'] for f in RX_FYJL}
                dispensed = [f for f in RX_FYJL]
                unfy = [c for c in RX_CFMAIN
                        if c['jsrq'] is not None and not c['iszf'] and c['id'] not in fyjl_ids]
                return {
                    'fy_count': len(dispensed),
                    'fy_total': sum((f['bmoney'] for f in dispensed), Decimal('0')),
                    'fy_refund_total': sum((f['bmoney'] for f in dispensed if f['bmoney'] < 0), Decimal('0')),
                    'fy_positive_total': sum((f['bmoney'] for f in dispensed if f['bmoney'] > 0), Decimal('0')),
                    'unfy_count': len(unfy),
                    'unfy_total': sum((c['bmoney'] for c in unfy), Decimal('0')) if unfy else None,
                }
            if "FROM dbo.his_zycz" in self._query:
                return {'cz_count': 0, 'cz_total': Decimal('0.00'),
                        'cz_refund_total': Decimal('0.00'), 'cz_positive_total': Decimal('0.00')}
            return None
        def close(self):
            pass

    class RxMockReader(HISReader):
        def __init__(self):
            self.cursor_obj = RxMockCursor()
        def get_connection(self):
            outer = self
            class MockConn:
                def cursor(self, as_dict=True):
                    return outer.cursor_obj
                def autocommit(self, value):
                    pass
                def commit(self):
                    pass
                def rollback(self):
                    pass
                def close(self):
                    pass
            return MockConn()

    rx_reader = RxMockReader()
    rx_detail = rx_reader.get_case_fee_detail('zy_synth_rx')
    # 7a. 未发药处方：仅「已计费+未作废+无发药记录」的 12.00 计入，笔数恰为 1
    assert rx_detail['fy_stat']['undispensed'] == Decimal('12.00')
    assert rx_detail['fy_stat']['undispensed_count'] == 1
    print("PASS [Suite 7a]: 已计费未发药处方精确计入（12.00 元 / 1 笔），未计费(99)与作废(55)正确排除")

    # 7b. 发药明细：含退药冲减，30.00 - 5.00 = 25.00
    assert rx_detail['fy_stat']['total'] == Decimal('25.00')
    assert rx_detail['fy_stat']['refund'] == Decimal('-5.00')
    print("PASS [Suite 7b]: 发药明细含退款冲减（30.00 - 5.00 = 25.00），与未发药不重复计数")

    # 7c. 一致性时点：两组聚合由同一条 SQL 语句完成（每次调用恰好 1 条合并语句）
    assert rx_reader.cursor_obj.combined_query_calls == 1
    print("PASS [Suite 7c]: 发药+未发药两组金额取自同一条 SQL 语句（语句级一致快照，无读间漏算窗口）")

    # 7d. 对账判据：25.00 + 12.00 = 37.00 == yfmoney，时序差正确豁免；
    #     若主单为 50.00（真实差额 13.00），判据正确暴露、绝不放行
    fy_ok = abs(rx_detail['fy_stat']['total'] + rx_detail['fy_stat']['undispensed'] - Decimal('37.00')) <= Decimal('0.10')
    fy_bad = abs(rx_detail['fy_stat']['total'] + rx_detail['fy_stat']['undispensed'] - Decimal('50.00')) <= Decimal('0.10')
    assert fy_ok is True and fy_bad is False
    print("PASS [Suite 7d]: 时序差（+12.00）正确豁免，真实差额（13.00 > 0.10）正确暴露")
    print("  [PASS] Suite 7 Undispensed-prescription Scope Verified.")

    # -------------------------------------------------------------
    # Test Suite 8: 读取器实际生成 SQL 的离线执行与变异对照（RECON-TEST-001）
    # 捕获 get_case_fee_detail 实际下发的药房合并聚合 SQL，在 SQLite 内存库原样执行：
    # 合成已发药/退款/已计费未发药/未计费/作废五类记录分别断言；
    # 删除作废过滤条件的变异查询必须得出不同结果（杜绝“桩在 Python 重算过滤条件”失真）
    # -------------------------------------------------------------
    print("\n[Suite 8] Executing reader's actual pharmacy SQL offline (SQLite) with mutation control...")

    class RecCursor:
        def __init__(self):
            self.sqls = []
        def execute(self, sql, params=()):
            self.sqls.append(sql)
        def fetchall(self):
            q = self.sqls[-1]
            if "FROM dbo.his_zyjz" in q:
                return [{'id': 301, 'lsh': 'zy_synth_sql', 'amoney': Decimal('200.00'),
                         'czmoney': Decimal('0.00'), 'yjmoney': Decimal('0.00'),
                         'yfmoney': Decimal('37.00'), 'iszf': 0}]
            if "FROM dbo.his_zycjz" in q:
                return [{'xmsflxid': '01', 'amoney': Decimal('200.00'), 'ssmoney': Decimal('200.00')}]
            return []
        def fetchone(self):
            q = self.sqls[-1]
            if "unfy_count" in q:
                return {'fy_count': 0, 'fy_total': None, 'fy_refund_total': None,
                        'fy_positive_total': None, 'unfy_count': 0, 'unfy_total': None}
            if "FROM dbo.his_zycz" in q:
                return {'cz_count': 0, 'cz_total': Decimal('0'), 'cz_refund_total': Decimal('0'), 'cz_positive_total': Decimal('0')}
            return None
        def close(self):
            pass

    class RecConn:
        def __init__(self):
            self.cur = RecCursor()
        def cursor(self, as_dict=True):
            return self.cur
        def autocommit(self, value):
            pass
        def commit(self):
            pass
        def rollback(self):
            pass
        def close(self):
            pass

    class RecReader(HISReader):
        def __init__(self):
            self.conn_obj = RecConn()
        def get_connection(self):
            return self.conn_obj

    rec_reader = RecReader()
    rec_reader.get_case_fee_detail('zy_synth_sql')
    fy_sql_list = [s for s in rec_reader.conn_obj.cur.sqls if 'unfy_count' in s]
    assert len(fy_sql_list) == 1, "药房聚合应为一条合并 SQL（语句内同时点）"
    fy_sql = fy_sql_list[0]

    import sqlite3
    lite = sqlite3.connect(':memory:')
    lc = lite.cursor()
    lc.execute("CREATE TABLE his_cfmain_fyjl (lsh TEXT, id TEXT, bmoney REAL)")
    lc.execute("CREATE TABLE his_cfmain (lsh TEXT, id TEXT, bmoney REAL, jsrq TEXT, iszf INTEGER)")
    lc.executemany("INSERT INTO his_cfmain_fyjl VALUES (?,?,?)", [
        ('zy_synth_sql', 'RX-DISP-0001', 30.00),
        ('zy_synth_sql', 'RX-DISP-0001', -5.00),
    ])
    lc.executemany("INSERT INTO his_cfmain VALUES (?,?,?,?,?)", [
        ('zy_synth_sql', 'RX-DISP-0001', 30.00, '2026-11-24 09:12:00', None),  # 已发药
        ('zy_synth_sql', 'RX-UNFY-0001', 12.00, '2026-11-24 09:30:00', None),  # 已计费未发药
        ('zy_synth_sql', 'RX-UNBILL-01', 99.00, None, None),                   # 未计费弃用
        ('zy_synth_sql', 'RX-VOID-0001', 55.00, '2026-11-24 09:40:00', 1),     # 作废
    ])
    adapted_sql = fy_sql.replace('dbo.', '').replace('%s', '?')
    lc.execute(adapted_sql, ('zy_synth_sql', 'zy_synth_sql'))
    row = lc.fetchone()
    # 列序：fy_count, fy_total, fy_refund_total, fy_positive_total, unfy_count, unfy_total
    assert row[0] == 2 and abs(row[1] - 25.00) < 1e-9, f"发药净额应为 25.00，实际 {row[1]}"
    assert abs(row[2] - (-5.00)) < 1e-9 and abs(row[3] - 30.00) < 1e-9, "退款/正向拆分不符"
    assert row[4] == 1 and abs(row[5] - 12.00) < 1e-9, f"未发药应为 1 笔 12.00，实际 {row[4]} 笔 {row[5]}"
    print("PASS [Suite 8a]: 实际 SQL 在 SQLite 原样执行——发药净额 25.00（含退款 -5.00）、未发药 1 笔 12.00，未计费/作废正确排除")

    # 变异对照：删除作废过滤条件，结果必须改变（证明断言确实依赖该过滤）
    mutated_sql = adapted_sql.replace("AND (c.iszf = 0 OR c.iszf IS NULL)", "AND (1 = 1)")
    assert mutated_sql != adapted_sql, "变异未生效"
    lc.execute(mutated_sql, ('zy_synth_sql', 'zy_synth_sql'))
    mrow = lc.fetchone()
    assert abs(mrow[5] - 67.00) < 1e-9 and abs(mrow[5] - row[5]) > 1e-9, "删除作废过滤的变异查询必须失败（计入作废 55.00）"
    lite.close()
    print("PASS [Suite 8b]: 变异对照——删除作废过滤条件后未发药变为 67.00，原查询断言对该变异敏感")
    print("  [PASS] Suite 8 Actual-SQL Offline Execution & Mutation Verified.")

    # -------------------------------------------------------------
    # Test Suite 9: 真实 get_case_fee_detail 双会话一致性验收（RECON-RX-001 深化 / TEST-ISO-001）
    # 每轮运行在院内隔离 SQL Server 上创建专有可丢弃数据库（rxt_recon_<随机>，零并发冲突，
    # 严禁指向共享/生产库），建与生产同名表，走真实 get_case_fee_detail 代码路径；
    # 写线程在读取方主单读就绪信号后才并发提交主单与药费调整，断言读取方只得到
    # 完整旧账或完整新账；变异子类关闭隔离保护后必须读到跨时点撕裂（证明验收对保护敏感）。
    # 环境缺失时明确报告 SKIP，绝不静默跳过。
    # -------------------------------------------------------------
    print("\n[Suite 9] Real get_case_fee_detail two-session consistency on per-run scratch database...")
    try:
        import pymssql
        import threading
        import uuid
    except ImportError:
        print("  [SKIP] Suite 9: pymssql 不可用（离线环境跳过，服务器上必跑）")
        skipped_suites.append('Suite 9 (pymssql 不可用)')
    else:
        _h = os.environ.get('RECON_TEST_DB_HOST')
        _port = int(os.environ.get('RECON_TEST_DB_PORT', '1433'))
        _u = os.environ.get('RECON_TEST_DB_USER')
        _p = os.environ.get('RECON_TEST_DB_PASS')
        # TEST-ISO-001：写型验收只认专用测试实例配置 RECON_TEST_DB_*，
        # 与日常 LOCAL_DB_* 完全分离——仅配置日常凭据时本套件写入次数为零（直接 SKIP）；
        # 专有数据库 rxt_recon_<随机> 每轮创建并销毁，即一次性对象
        _db = os.environ.get('RECON_TEST_DB_NAME')
        if not (_h and _u and _p and _db):
            print("  [SKIP] Suite 9: RECON_TEST_DB_* 专用测试实例未配置（仅日常 LOCAL_DB_* 时绝不写入，服务器上必跑）")
            skipped_suites.append('Suite 9 (RECON_TEST_DB_* 专用测试实例未配置)')
        else:
            if _db.strip().lower() in ('his', 'hisdb', 'master', 'model', 'msdb'):
                raise PermissionError(
                    f"Suite 9 目标库校验失败：RECON_TEST_DB_NAME={_db!r} 命中共享/生产/系统库拒绝名单，"
                    "写型验收只准指向院内隔离测试实例库")
            scratch = 'rxt_recon_' + uuid.uuid4().hex[:8]
            admin = pymssql.connect(server=_h, port=_port, user=_u, password=_p, database=_db,
                                    autocommit=True, login_timeout=10, timeout=30)
            ac = admin.cursor()
            ac.execute(f"CREATE DATABASE {scratch}")
            try:
                # 专库内建与生产同名表，读取器实际 SQL 原样执行，不做任何表名替换
                ddl = pymssql.connect(server=_h, port=_port, user=_u, password=_p, database=scratch,
                                      autocommit=True, login_timeout=10, timeout=30)
                dc = ddl.cursor()
                dc.execute("CREATE TABLE dbo.his_zyjz (lsh NVARCHAR(64), id NVARCHAR(64), amoney DECIMAL(18,5), czmoney DECIMAL(18,5), yjmoney DECIMAL(18,5), yfmoney DECIMAL(18,5), iszf INT, jzrq DATETIME2)")
                dc.execute("CREATE TABLE dbo.his_zycjz (lsh NVARCHAR(64), id NVARCHAR(64), xmsflxid NVARCHAR(16), amoney DECIMAL(18,5), ssmoney DECIMAL(18,5))")
                dc.execute("CREATE TABLE dbo.his_zycz (lsh NVARCHAR(64), money DECIMAL(18,5))")
                dc.execute("CREATE TABLE dbo.his_cfmain (lsh NVARCHAR(64), id NVARCHAR(64), bmoney DECIMAL(18,5), jsrq DATETIME2 NULL, iszf INT NULL)")
                dc.execute("CREATE TABLE dbo.his_cfmain_fyjl (lsh NVARCHAR(64), id NVARCHAR(64), bmoney DECIMAL(18,5))")
                ddl.close()

                def _seed_old_account():
                    """灌入合成旧账（编号全部虚构）：主单 37 = 已发药净额 25（30 - 5 退药）+ 已计费未发药 12"""
                    sd = pymssql.connect(server=_h, port=_port, user=_u, password=_p, database=scratch,
                                         autocommit=True, login_timeout=10, timeout=30)
                    sc = sd.cursor()
                    for t in ('his_zyjz', 'his_zycjz', 'his_zycz', 'his_cfmain', 'his_cfmain_fyjl'):
                        sc.execute(f"DELETE FROM dbo.{t}")
                    sc.execute("INSERT INTO dbo.his_zyjz VALUES ('RXTEST01','S1',200,0,0,37,0,SYSDATETIME())")
                    sc.execute("INSERT INTO dbo.his_zycjz VALUES ('RXTEST01','S1','01',200,200)")
                    sc.execute("INSERT INTO dbo.his_cfmain VALUES ('RXTEST01','RX-DISP-0001',30,'2026-11-24 09:12:00',NULL)")
                    sc.execute("INSERT INTO dbo.his_cfmain VALUES ('RXTEST01','RX-UNFY-0001',12,'2026-11-24 09:30:00',NULL)")
                    sc.execute("INSERT INTO dbo.his_cfmain VALUES ('RXTEST01','RX-UNBILL-01',99,NULL,NULL)")
                    sc.execute("INSERT INTO dbo.his_cfmain VALUES ('RXTEST01','RX-VOID-0001',55,'2026-11-24 09:40:00',1)")
                    sc.execute("INSERT INTO dbo.his_cfmain_fyjl VALUES ('RXTEST01','RX-DISP-0001',30)")
                    sc.execute("INSERT INTO dbo.his_cfmain_fyjl VALUES ('RXTEST01','RX-DISP-0001',-5)")
                    sd.close()

                class HookCursor:
                    """包装游标：主单读取完成后触发就绪钩子（置位事件并按需停顿），
                    让写线程在读取事务持锁期间发起并发提交；其余调用原样转发"""
                    def __init__(self, inner, on_master_read):
                        self._inner = inner
                        self._sql = ""
                        self._hook = on_master_read
                    def execute(self, sql, params=()):
                        self._sql = sql
                        return self._inner.execute(sql, params)
                    def fetchall(self):
                        rows = self._inner.fetchall()
                        if self._hook and "FROM dbo.his_zyjz " in self._sql:
                            self._hook()
                        return rows
                    def fetchone(self):
                        return self._inner.fetchone()

                class HookConn:
                    def __init__(self, conn, hook):
                        self._conn = conn
                        self._hook = hook
                    def cursor(self, as_dict=False):
                        return HookCursor(self._conn.cursor(as_dict=as_dict), self._hook)
                    def autocommit(self, value):
                        return self._conn.autocommit(value)
                    def commit(self):
                        return self._conn.commit()
                    def rollback(self):
                        return self._conn.rollback()
                    def close(self):
                        return self._conn.close()

                class ScratchReader(HISReader):
                    """真实 get_case_fee_detail 代码路径，仅连接目标换为本轮专有库；
                    isolation=None 时经 RECON_ISOLATION 变异点关闭显式隔离保护（仅限本测试）"""
                    def __init__(self, isolation='SERIALIZABLE', master_read_event=None, wait_event=None, sync_stage=''):
                        self._event = master_read_event
                        self._wait = wait_event
                        self._sync_stage = sync_stage
                        if isolation is None:
                            self.RECON_ISOLATION = None  # 实例级变异，类属性生产默认不受影响
                    def get_connection(self):
                        conn = pymssql.connect(server=_h, port=_port, user=_u, password=_p,
                                               database=scratch, charset='cp936',
                                               login_timeout=10, timeout=60)
                        if self._event is None:
                            return conn
                        def _hook():
                            self._event.set()      # 写线程就绪同步：主单已读、锁已持有
                            if self._wait is not None:
                                # 事件同步替代固定休眠：等待写线程信号（保护轮=已发起，变异轮=已提交），
                                # 慢连接下结果仍可重复；超时必须显式报告轮次与失败阶段，
                                # 绝不把同步失败误报为对账断言失败（RECON-RX-001 验收要求）
                                if not self._wait.wait(timeout=25):
                                    raise TimeoutError(
                                        f"Suite 9 同步超时：{self._sync_stage} —— 25 秒内未等到写线程信号，"
                                        "判定为同步失败（请检查隔离实例连接与写线程），而非对账断言失败")
                        return HookConn(conn, _hook)

                def _writer(event, results, attempted=None, committed=None):
                    """并发写入：等待读取方主单读就绪信号后，提交主单+药费调整（新账 45 = 33 + 12）；
                    attempted 在即将发起写时置位（保护轮同步写尝试），committed 在写线程结束时置位（变异轮同步提交完成）"""
                    try:
                        if not event.wait(timeout=20):
                            results.append('error:no-read-ready-signal')
                            return
                        W = pymssql.connect(server=_h, port=_port, user=_u, password=_p, database=scratch,
                                            autocommit=False, login_timeout=10, timeout=60)
                        if attempted is not None:
                            attempted.set()
                        wc = W.cursor()
                        wc.execute("UPDATE dbo.his_zyjz SET yfmoney = 45 WHERE lsh = 'RXTEST01'")
                        wc.execute("INSERT INTO dbo.his_cfmain_fyjl VALUES ('RXTEST01','RX-DISP-0001',8)")
                        W.commit()
                        results.append('committed')
                        W.close()
                    except Exception as e:
                        results.append('error:' + type(e).__name__)
                    finally:
                        if committed is not None:
                            committed.set()  # 无论成败均放行读取方，成败由 results 断言

                # —— 9a/9b：SERIALIZABLE 保护下只读完整旧账或完整新账 ——
                _seed_old_account()
                ev1 = threading.Event()
                att1 = threading.Event()
                w1 = []
                wt1 = threading.Thread(target=_writer, args=(ev1, w1), kwargs={'attempted': att1})
                wt1.start()
                r1 = ScratchReader(isolation='SERIALIZABLE', master_read_event=ev1, wait_event=att1,
                                   sync_stage='9a/9b 保护轮：等待写线程已发起信号')
                d_old = r1.get_case_fee_detail('RXTEST01')   # 真实方法，含 SERIALIZABLE 显式事务
                wt1.join(timeout=30)
                assert w1 == ['committed'], f"写入会话未正常完成: {w1}"
                old_master = Decimal(str(d_old['zyjz']['yfmoney']))
                assert old_master == Decimal('37') and d_old['fy_stat']['total'] == Decimal('25.00') \
                    and d_old['fy_stat']['undispensed'] == Decimal('12.00'), \
                    f"串行化读取必须得到完整旧账，实际主单 {old_master} / 发药 {d_old['fy_stat']['total']} / 未发药 {d_old['fy_stat']['undispensed']}"
                print("PASS [Suite 9a]: 真实 get_case_fee_detail 在并发提交期间读到完整旧账（主单 37 = 发药 25 + 未发药 12）")

                d_new = ScratchReader().get_case_fee_detail('RXTEST01')
                new_master = Decimal(str(d_new['zyjz']['yfmoney']))
                assert new_master == Decimal('45') and d_new['fy_stat']['total'] == Decimal('33.00') \
                    and d_new['fy_stat']['undispensed'] == Decimal('12.00'), \
                    f"写入提交后必须读到完整新账，实际主单 {new_master} / 发药 {d_new['fy_stat']['total']} / 未发药 {d_new['fy_stat']['undispensed']}"
                print("PASS [Suite 9b]: 写入提交后新读取只见完整新账（主单 45 = 发药 33 + 未发药 12），绝不出现在中间态")

                # —— 9c：变异对照——去除隔离保护（RECON_ISOLATION=None）必须读到不一致 ——
                _seed_old_account()
                ev2 = threading.Event()
                com2 = threading.Event()
                w2 = []
                wt2 = threading.Thread(target=_writer, args=(ev2, w2), kwargs={'committed': com2})
                wt2.start()
                r2 = ScratchReader(isolation=None, master_read_event=ev2, wait_event=com2,
                                   sync_stage='9c 变异轮：等待写线程提交完成事件')
                d_mut = r2.get_case_fee_detail('RXTEST01')   # 默认 READ COMMITTED；药费读取前等待写提交事件
                wt2.join(timeout=30)
                assert w2 == ['committed'], f"变异轮写入会话未正常完成: {w2}"
                mut_master = Decimal(str(d_mut['zyjz']['yfmoney']))
                mut_fy = d_mut['fy_stat']['total'] + d_mut['fy_stat']['undispensed']
                assert mut_master != mut_fy, \
                    f"去除隔离保护的变异读取必须暴露不一致（主单 {mut_master} vs 药费 {mut_fy}），否则验收对保护不敏感"
                print(f"PASS [Suite 9c]: 变异对照——关闭隔离后读到跨时点撕裂账（主单 {mut_master} ≠ 药费 {mut_fy}），验收对隔离保护确实敏感")
            finally:
                try:
                    ac.execute(f"ALTER DATABASE {scratch} SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
                except Exception:
                    pass
                ac.execute(f"DROP DATABASE {scratch}")
                admin.close()
            print("  [PASS] Suite 9 Real-reader Two-session Consistency & Mutation Verified.")

    print("\n" + "=" * 70)
    if skipped_suites:
        # 未执行的套件必须计入 SKIP 并如实报告，绝不输出“全套通过”（RECON-RX-001 验收要求）
        print(f"  EXECUTED SUITES PASSED; SKIPPED {len(skipped_suites)} 项: " + "; ".join(skipped_suites))
    else:
        print("  ALL 9 RECONCILIATION SUITES PASSED (100.0% COMPLIANT WITH v1.2)")
    print("=" * 70)

if __name__ == '__main__':
    run_settled_reconciliation_suite()

# -*- coding: utf-8 -*-
"""
Inpatient DIP Real-time Monitoring Scanner
(在院患者 DIP 实时预警监控扫描器)

Strictly compliant with 《开发设计文档 v1.2》:
1. Dynamic Rule Loading: Reads dip_rules from local SQL Server 2022 his_agent_db.
   Strictly forbids hardcoded coefficients (e.g. 7.5*0.6 / 7.0*0.6).
2. High-precision Decimal Arithmetic: Uses Decimal unrounded cross-multiplication
   for traffic lights (<89.99% GREEN, 90%~110% YELLOW [endpoints inclusive], >110% RED).
   Marginal cases like 110.004% are annotated with "略高于 110%".
3. Status Isolation:
   - Self-pay patients marked NOT_APPLICABLE.
   - Ambiguous / multi-candidate / missing params / missing bed days marked PENDING with pending_reason.
4. Idempotency & Batch Control: Generates unique batch_id, records data_cutoff_time,
   and performs atomic idempotent upserts into dbo.inpatient_dip_snapshot.
"""
import os
import sys
import re
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Dict, Any, Optional
import pymssql

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from db.local_db import get_local_db_connection, query_approved_dip_rules
from db.his_reader import HISReader
from tools.dip_calculator import match_dip_rule, evaluate_traffic_light, get_loaded_rules, normalize_insurance_type

# Timezone helper for Beijing (UTC+8)
BEIJING_TZ = timezone(timedelta(hours=8))

class InpatientScanner:
    def __init__(self, rule_version: str = "2026_SAMPLE_V1"):
        self.rule_version = rule_version
        self.his_reader = HISReader()

    def map_insurance_type(self, ylzffs: Optional[str], fylx: Optional[str]) -> str:
        """Map HIS payment/insurance codes to standard insurance category."""
        if fylx:
            norm = normalize_insurance_type(fylx)
            if norm["valid"]:
                return norm["category"]
            elif norm["reason"].startswith("UNSUPPORTED_INS_TYPE"):
                return fylx.strip()
            elif norm["reason"] == "AMBIGUOUS_INS_TYPE":
                return "AMBIGUOUS_城镇"

        if ylzffs:
            s = str(ylzffs).strip()
            if s == "1":
                return "职工"
            if s == "2":
                return "居民"
            if s == "7":
                return "自费"
            if s == "3":
                return "公费医疗"
            return f"OTHER_{s}"

        return "UNKNOWN"

    def parse_age(self, age_str: Any) -> Optional[int]:
        """Extract integer age from '56岁' or integer."""
        if age_str is None:
            return None
        if isinstance(age_str, (int, float)):
            return int(age_str)
        m = re.search(r'\d+', str(age_str))
        return int(m.group()) if m else None

    def scan_and_snapshot(
        self,
        batch_id: Optional[str] = None,
        department: Optional[str] = None,
        limit: int = 50,
        include_recent_settled: bool = False
    ) -> Dict[str, Any]:
        """
        Executes a scan over inpatient cases and saves snapshots into dbo.inpatient_dip_snapshot.
        If include_recent_settled is True, also evaluates recent discharges for validation.
        """
        now_bj = datetime.now(BEIJING_TZ)
        if not batch_id:
            batch_id = f"BATCH_{now_bj.strftime('%Y%m%d_%H%M%S')}"

        data_cutoff_time = now_bj.strftime("%Y-%m-%d %H:%M:%S")

        # 1. Fetch patients to evaluate
        patients_to_scan = []
        active_inpatients = self.his_reader.get_active_inpatients(department=department, limit=limit)
        patients_to_scan.extend(active_inpatients)

        # Strictly decouple: Only include settled cases when explicitly requested (e.g. for replay/backtesting)
        if include_recent_settled:
            settled_cases = self.his_reader.get_settled_cases(limit=limit, department=department)
            for sc in settled_cases:
                # Avoid duplicate scan if patient already active
                if not any(p['zyh'] == sc['lsh'] for p in patients_to_scan):
                    patients_to_scan.append({
                        'zyh': sc['lsh'],
                        'patient_name': sc['patient_name'],
                        'gender': sc['gender'],
                        'age': sc['age'],
                        'current_dept': sc['discharge_dept'] or sc['admission_dept'] or '未知科室',
                        'current_bed': '已结算',
                        'admission_time': sc['admission_time'],
                        'current_stay_days': sc['stay_days'],
                        'attending_doctor': sc['attending_doctor'] or sc['doctor_code'] or '未知医生',
                        'dept_director': sc['dept_director'] or '未知主任',
                        'admission_diag_code': sc['primary_diag_code'],
                        'admission_diag_name': sc['primary_diag_name'],
                        'ssmc1': sc['ssmc1'],
                        'ssmc2': sc['ssmc2'],
                        'ssmc3': sc['ssmc3'],
                        'ylzffs': sc.get('ylzffs'),
                        'insurance_type': sc['insurance_type'],
                        '_pre_settled_amount': sc['total_amount']
                    })

        print(f"[{batch_id}] Total patients selected for DIP snapshot: {len(patients_to_scan)}")

        # 2. Process DIP evaluation for each patient
        evaluated_snapshots = []
        summary = {
            'batch_id': batch_id,
            'rule_version': self.rule_version,
            'data_cutoff_time': data_cutoff_time,
            'total_scanned': len(patients_to_scan),
            'GREEN': 0,
            'YELLOW': 0,
            'RED': 0,
            'NOT_APPLICABLE': 0,
            'PENDING': 0
        }

        local_cur, local_conn = get_local_db_connection(as_dict=True)

        try:
            for pat in patients_to_scan:
                zyh = pat['zyh']
                ward_name = pat.get('current_dept') or '默认病区'
                bed_no = str(pat.get('current_bed') or '-')
                dept_name = pat.get('current_dept') or '默认科室'
                dept_id = str(pat.get('current_dept_id') or pat.get('dept_id') or '')
                if not dept_id and dept_name == '01中医科':
                    dept_id = '4'
                doctor_id = str(pat.get('attending_doctor_id') or pat.get('attending_doctor') or 'UNKNOWN')
                age = self.parse_age(pat.get('age'))
                ins_type = self.map_insurance_type(pat.get('ylzffs'), pat.get('insurance_type'))

                # Calculate current fee
                has_fee_activity = True
                if '_pre_settled_amount' in pat:
                    current_fee = Decimal(str(pat['_pre_settled_amount']))
                else:
                    fee_info = self.his_reader.get_active_patient_fee(zyh, cutoff_time=data_cutoff_time)
                    current_fee = fee_info['total_fee']
                    has_fee_activity = fee_info['has_activity']

                # Diagnosis & Procedures
                diag_code = (pat.get('admission_diag_code') or '').strip()
                diag_name = (pat.get('admission_diag_name') or '').strip()
                procedures = [pat.get('ssmc1'), pat.get('ssmc2'), pat.get('ssmc3')]
                op_kw = " ".join([p for p in procedures if p]) or None
                bed_days = self.parse_age(pat.get('current_stay_days'))

                # Primary Evaluation logic
                target_base = None
                fee_ratio = None
                matched_rule_id = None
                alert_level = "PENDING"
                pending_reason = None

                norm_ins = normalize_insurance_type(ins_type)

                if not norm_ins["valid"]:
                    alert_level = "PENDING"
                    pending_reason = norm_ins["reason"]
                elif norm_ins["category"] == "自费":
                    alert_level = "NOT_APPLICABLE"
                    pending_reason = "SELF_PAY"
                elif not diag_code and not diag_name and not op_kw:
                    alert_level = "PENDING"
                    pending_reason = "MISSING_DIAGNOSIS"
                elif not has_fee_activity and current_fee == Decimal('0.00'):
                    # Data incomplete or no fee activity yet - do NOT falsely classify as GREEN
                    alert_level = "PENDING"
                    pending_reason = "NO_FEE_ACTIVITY"
                else:
                    disease_query = diag_code if diag_code else diag_name
                    match_res = match_dip_rule(
                        disease=disease_query,
                        age=age,
                        ins_type=norm_ins["category"],
                        op_keyword=op_kw,
                        bed_days=bed_days,
                        rule_version=self.rule_version
                    )

                    if match_res.get("matched"):
                        matched_rule_id = match_res.get("rule_id")
                        bench_val = match_res.get("benchmark_amount")
                        target_base = Decimal(str(bench_val)) if bench_val is not None else None

                        if target_base is None or target_base <= Decimal('0'):
                            alert_level = "PENDING"
                            pending_reason = "INVALID_BENCHMARK"
                            fee_ratio = None
                        else:
                            low_th = Decimal(str(match_res.get("low_threshold") or "0.9000"))
                            high_th = Decimal(str(match_res.get("high_threshold") or "1.1000"))

                            eval_res = evaluate_traffic_light(
                                current_cost=current_fee,
                                benchmark=target_base,
                                low_threshold=low_th,
                                high_threshold=high_th,
                                ins_type=ins_type
                            )

                            light = eval_res.get("light")
                            if light == "绿灯":
                                alert_level = "GREEN"
                            elif light == "黄灯":
                                alert_level = "YELLOW"
                            elif light == "红灯":
                                alert_level = "RED"
                            else:
                                alert_level = "PENDING"

                            fee_ratio = (current_fee / target_base).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
                            pending_reason = eval_res.get("boundary_note")
                    else:
                        alert_level = "PENDING"
                        pending_reason = match_res.get("pending_reason", match_res.get("status", "UNMATCHED"))

                summary[alert_level] = summary.get(alert_level, 0) + 1

                # 3. Idempotent Upsert into dbo.inpatient_dip_snapshot
                upsert_sql = """
                MERGE dbo.inpatient_dip_snapshot AS target
                USING (SELECT %s AS batch_id, %s AS visit_id) AS source
                ON (target.batch_id = source.batch_id AND target.visit_id = source.visit_id)
                WHEN MATCHED THEN
                    UPDATE SET
                        ward_name = %s,
                        bed_no = %s,
                        dept_id = %s,
                        dept_name = %s,
                        doctor_id = %s,
                        age = %s,
                        ins_type = %s,
                        primary_icd = %s,
                        matched_rule_id = %s,
                        rule_version = %s,
                        current_fee = %s,
                        target_base = %s,
                        fee_ratio = %s,
                        alert_level = %s,
                        pending_reason = %s,
                        discharge_review = %s,
                        data_cutoff_time = %s,
                        synced_at = SYSDATETIME()
                WHEN NOT MATCHED THEN
                    INSERT (
                        batch_id, visit_id, ward_name, bed_no, dept_id, dept_name, doctor_id, age,
                        ins_type, primary_icd, matched_rule_id, rule_version, current_fee,
                        target_base, fee_ratio, alert_level, pending_reason, discharge_review,
                        data_cutoff_time, synced_at
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, SYSDATETIME()
                    );
                """
                params = (
                    # MATCHED params
                    batch_id, zyh,
                    ward_name, bed_no, dept_id, dept_name, doctor_id, age,
                    ins_type, diag_code or None, matched_rule_id, self.rule_version,
                    current_fee, target_base, fee_ratio, alert_level, pending_reason,
                    0, data_cutoff_time,
                    # NOT MATCHED params
                    batch_id, zyh, ward_name, bed_no, dept_id, dept_name, doctor_id, age,
                    ins_type, diag_code or None, matched_rule_id, self.rule_version,
                    current_fee, target_base, fee_ratio, alert_level, pending_reason,
                    0, data_cutoff_time
                )
                local_cur.execute(upsert_sql, params)

                evaluated_snapshots.append({
                    'visit_id': zyh,
                    'patient_name': pat.get('patient_name'),
                    'dept_name': dept_name,
                    'doctor_id': doctor_id,
                    'ins_type': ins_type,
                    'primary_icd': diag_code,
                    'current_fee': float(current_fee),
                    'target_base': float(target_base) if target_base is not None else None,
                    'fee_ratio': float(fee_ratio) if fee_ratio is not None else None,
                    'alert_level': alert_level,
                    'pending_reason': pending_reason
                })

            local_conn.commit()
            print(f"[{batch_id}] Successfully persisted {len(evaluated_snapshots)} snapshot records to local_db.")
        finally:
            local_conn.close()

        summary['snapshots'] = evaluated_snapshots
        return summary

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Inpatient DIP Scanner")
    parser.add_argument("--batch-id", type=str, default=None, help="Custom batch ID")
    parser.add_argument("--dept", type=str, default=None, help="Filter by department")
    parser.add_argument("--limit", type=int, default=50, help="Scan limit")
    parser.add_argument("--settled", action="store_true", help="Include recently settled patients")
    args = parser.parse_args()

    scanner = InpatientScanner()
    res = scanner.scan_and_snapshot(
        batch_id=args.batch_id,
        department=args.dept,
        limit=args.limit,
        include_recent_settled=args.settled
    )

    print("\n" + "=" * 60)
    print("  INPATIENT SCANNER RUN SUMMARY")
    print("=" * 60)
    print(f"  Batch ID:          {res['batch_id']}")
    print(f"  Rule Version:      {res['rule_version']}")
    print(f"  Cutoff Time:       {res['data_cutoff_time']}")
    print(f"  Total Scanned:     {res['total_scanned']}")
    print(f"  GREEN (<90%):      {res['GREEN']}")
    print(f"  YELLOW (90%-110%): {res['YELLOW']}")
    print(f"  RED (>110%):       {res['RED']}")
    print(f"  NOT_APPLICABLE:    {res['NOT_APPLICABLE']}")
    print(f"  PENDING:           {res['PENDING']}")
    print("=" * 60)

if __name__ == '__main__':
    main()

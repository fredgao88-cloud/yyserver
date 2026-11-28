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
import uuid
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

    @staticmethod
    def infer_op_keyword_from_billed_items(diag_code: str, diag_name: str, billed_items: Any) -> Optional[str]:
        """
        根据 his_zycz 净计费处置项目识别明确且无歧义的 DIP 操作关键字（仅在首页手术编码为空时启用）。
        若存在切除/活检/EMR/ESD 等高阶治疗项，绝不降级为单纯内镜检查，交由人工复核 (PENDING)。
        """
        if not isinstance(billed_items, (list, tuple)) or not billed_items:
            return None
        items = [str(x).strip() for x in billed_items if x]
        if not items:
            return None

        joined = " ".join(items)
        # 若已计费切除/活检/内镜治疗等操作，不简化为单纯内镜检查
        if any(k in joined for k in ("切除", "活检", "活组织检查", "EMR", "ESD")):
            return None

        has_lower_scope = any(("下消化道内镜检查" in x or "结肠镜检查" in x) for x in items)
        has_upper_scope = any(("上消化道内镜检查" in x or "胃镜检查" in x) for x in items)
        diag_text = f"{diag_code or ''} {diag_name or ''}".upper()

        if has_lower_scope and any(k in diag_text for k in ("K63", "K62", "D12", "结肠", "直肠", "大肠")):
            return "电子结肠镜检查"
        if has_upper_scope and any(k in diag_text for k in ("K21", "K22", "K29", "K31", "胃", "食管", "十二指肠")):
            return "胃镜检查"
        if has_lower_scope and not has_upper_scope:
            return "电子结肠镜检查"
        if has_upper_scope and not has_lower_scope:
            return "胃镜检查"
        return None

    def scan_and_snapshot(
        self,
        batch_id: Optional[str] = None,
        department: Optional[str] = None,
        limit: Optional[int] = None,
        include_recent_settled: bool = False,
        notify_dingtalk: bool = False,
        scope: str = "active",
        year_month: Optional[str] = None,
        if_exists: str = "replace",
    ) -> Dict[str, Any]:
        """
        Executes a scan over inpatient cases and saves snapshots into dbo.inpatient_dip_snapshot.
        - scope='active' (default): evaluates active un-discharged patients (default limit=50).
        - scope='month': evaluates all patients overlapping target year_month (full pagination when limit is None),
          writing into a distinct MONTH_YYYYMM_* batch so active-only snapshots stay strictly isolated.
        - if_exists: 'replace' (default, atomically replaces stale detail rows of an explicit batch_id)
          or 'error' (rejects reusing an existing explicit batch_id).
        """
        now_bj = datetime.now(BEIJING_TZ)
        scope_val = (scope or "active").strip().lower()
        if scope_val not in ("active", "month"):
            raise ValueError(f"Unsupported scan scope: {scope}")
        if if_exists not in ("replace", "error"):
            raise ValueError(f"Unsupported if_exists policy: {if_exists}")

        target_ym = (year_month or now_bj.strftime("%Y-%m")).strip()
        explicit_batch_id = bool(batch_id and str(batch_id).strip())
        if not explicit_batch_id:
            # SNAP-BATCH-ID-001：微秒 + 随机 hex 确保同秒/固定时钟并发调用绝不碰撞且保持前缀兼容（总长 <= 42）
            uniq_token = f"{now_bj.strftime('%Y%m%d_%H%M%S_%f')}_{uuid.uuid4().hex[:6]}"
            if scope_val == "month":
                ym_compact = target_ym.replace("-", "")
                batch_id = f"MONTH_{ym_compact}_{uniq_token}"
            else:
                batch_id = f"BATCH_{uniq_token}"
        else:
            batch_id = str(batch_id).strip()

        data_cutoff_time = now_bj.strftime("%Y-%m-%d %H:%M:%S")

        # 1. Fetch patients to evaluate
        patients_to_scan = []
        month_meta: Dict[str, Any] = {
            "year_month": target_ym if scope_val == "month" else None,
            "scan_limit": limit,
            "is_truncated": False,
            "probe_version": 2,
        }
        if scope_val == "month":
            try:
                monthly_res = self.his_reader.get_monthly_inpatients(
                    year_month=target_ym,
                    department=department,
                    limit=limit,
                    return_meta=True,
                )
            except TypeError:
                probe_lim = (int(limit) + 1) if limit is not None else None
                monthly_res = self.his_reader.get_monthly_inpatients(
                    year_month=target_ym,
                    department=department,
                    limit=probe_lim,
                )
            if isinstance(monthly_res, tuple) and len(monthly_res) == 2:
                monthly_rows, raw_meta = monthly_res
                month_meta.update(raw_meta or {})
            else:
                monthly_rows = list(monthly_res or [])
                if limit is not None and len(monthly_rows) > int(limit):
                    monthly_rows = monthly_rows[:int(limit)]
                    month_meta["is_truncated"] = True
            seen_zyh = set()
            for r in monthly_rows:
                zyh_val = r.get("zyh")
                if zyh_val and zyh_val in seen_zyh:
                    continue
                if zyh_val:
                    seen_zyh.add(zyh_val)
                patients_to_scan.append(r)
        else:
            active_limit = 50 if limit is None else int(limit)
            month_meta["scan_limit"] = active_limit
            try:
                active_res = self.his_reader.get_active_inpatients(
                    department=department,
                    limit=active_limit,
                    return_meta=True,
                )
            except TypeError:
                # 兼容旧版未实现 return_meta 参数的读取器：同样按 active_limit + 1 探针多取一条以精准判定截断
                probe_lim = (active_limit + 1) if active_limit is not None else None
                active_res = self.his_reader.get_active_inpatients(
                    department=department,
                    limit=probe_lim,
                )
            if isinstance(active_res, tuple) and len(active_res) == 2:
                active_inpatients, act_meta = active_res
                month_meta["is_truncated"] = bool((act_meta or {}).get("is_truncated"))
                if act_meta and "scan_limit" in act_meta:
                    month_meta["scan_limit"] = act_meta["scan_limit"]
            else:
                active_inpatients = list(active_res or [])
                if active_limit is not None and len(active_inpatients) > active_limit:
                    active_inpatients = active_inpatients[:active_limit]
                    month_meta["is_truncated"] = True
                else:
                    month_meta["is_truncated"] = False
            patients_to_scan.extend(active_inpatients)

            # Strictly decouple: Only include settled cases when explicitly requested (e.g. for replay/backtesting)
            if include_recent_settled:
                settled_cases = self.his_reader.get_settled_cases(limit=active_limit, department=department)
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
                            '_pre_settled_amount': sc['total_amount'],
                            '_is_settled': True,
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
            'PENDING': 0,
            'dept_alert_counts': {}
        }

        local_cur, local_conn = get_local_db_connection(as_dict=True)

        try:
            local_cur.execute(
                "IF OBJECT_ID(N'dbo.inpatient_dip_snapshot', N'U') IS NOT NULL "
                "AND COL_LENGTH('dbo.inpatient_dip_snapshot', 'doctor_name') IS NULL "
                "ALTER TABLE dbo.inpatient_dip_snapshot ADD doctor_name NVARCHAR(64) NULL;"
            )
            local_cur.execute(
                "IF OBJECT_ID(N'dbo.inpatient_scan_batch', N'U') IS NULL "
                "CREATE TABLE dbo.inpatient_scan_batch ("
                "batch_id NVARCHAR(64) PRIMARY KEY, "
                "seq_id BIGINT IDENTITY(1,1) NOT NULL, "
                "scope NVARCHAR(16) NOT NULL, "
                "year_month NVARCHAR(7) NULL, "
                "rule_version NVARCHAR(50) NOT NULL, "
                "scan_limit INT NULL, "
                "total_scanned INT NOT NULL, "
                "is_truncated BIT NOT NULL DEFAULT 0, "
                "probe_version INT NULL, "
                "data_cutoff_time DATETIME2 NOT NULL, "
                "synced_at DATETIME2 NOT NULL DEFAULT SYSDATETIME()"
                ");"
            )
            local_cur.execute(
                "IF OBJECT_ID(N'dbo.inpatient_scan_batch', N'U') IS NOT NULL "
                "AND COL_LENGTH('dbo.inpatient_scan_batch', 'probe_version') IS NULL "
                "ALTER TABLE dbo.inpatient_scan_batch ADD probe_version INT NULL;"
            )
            local_cur.execute(
                "IF OBJECT_ID(N'dbo.inpatient_scan_batch', N'U') IS NOT NULL "
                "AND COL_LENGTH('dbo.inpatient_scan_batch', 'seq_id') IS NULL "
                "ALTER TABLE dbo.inpatient_scan_batch ADD seq_id BIGINT IDENTITY(1,1) NOT NULL;"
            )
            if explicit_batch_id:
                if if_exists == "error":
                    local_cur.execute(
                        "SELECT TOP 1 1 FROM dbo.inpatient_scan_batch WHERE batch_id = %s",
                        (batch_id,),
                    )
                    meta_dup = local_cur.fetchone()
                    local_cur.execute(
                        "SELECT TOP 1 1 FROM dbo.inpatient_dip_snapshot WHERE batch_id = %s",
                        (batch_id,),
                    )
                    snap_dup = local_cur.fetchone()
                    if meta_dup or snap_dup:
                        raise ValueError(f"Duplicate explicit batch_id rejected: {batch_id}")
                else:
                    # SNAP-BATCH-ID-001：显式重用 batch_id 重试时，同事务先清理该批旧明细，保证摘要与明细 100% 一致
                    local_cur.execute(
                        "DELETE FROM dbo.inpatient_dip_snapshot WHERE batch_id = %s",
                        (batch_id,),
                    )
            for pat in patients_to_scan:
                zyh = pat['zyh']
                ward_name = pat.get('current_dept') or '默认病区'
                bed_no = str(pat.get('current_bed') or '-')
                dept_name = pat.get('current_dept') or '默认科室'
                dept_id = str(pat.get('current_dept_id') or pat.get('dept_id') or '').strip() or None
                doctor_id = str(pat.get('attending_doctor_id') or pat.get('attending_doctor') or 'UNKNOWN')
                doctor_name = str(pat.get('attending_doctor') or '').strip() or None
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
                clean_procs = [str(p).strip() for p in procedures if p and str(p).strip() and str(p).strip() != '-']
                op_kw = "+".join(clean_procs) or None
                if not op_kw and hasattr(self.his_reader, "get_active_billed_procedures"):
                    try:
                        billed_items = self.his_reader.get_active_billed_procedures(zyh)
                    except Exception:
                        billed_items = []
                    if isinstance(billed_items, (list, tuple)) and billed_items:
                        op_kw = self.infer_op_keyword_from_billed_items(diag_code, diag_name, billed_items)
                bed_days = self.parse_age(pat.get('current_stay_days'))
                discharge_review_flag = 1 if pat.get('_is_settled') else 0

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
                    # 若已有明确编码（如 I63.900 脑梗死）但规则库无该病种，严禁退回病名做子串模糊匹配（防“脑梗死”误配“脑梗死后遗症 I69.3”）；
                    # 仅当编码为空或非标准 ICD 编码时才回退临床名称。
                    has_std_code = bool(diag_code and re.match(r'^[A-Z]\d{2}', diag_code.upper()))
                    if not match_res.get("matched") and match_res.get("status") == "NOT_FOUND" and diag_code and diag_name and not has_std_code:
                        fallback_res = match_dip_rule(
                            disease=diag_name,
                            age=age,
                            ins_type=norm_ins["category"],
                            op_keyword=op_kw,
                            bed_days=bed_days,
                            rule_version=self.rule_version
                        )
                        if fallback_res.get("matched") or fallback_res.get("status") != "NOT_FOUND":
                            match_res = fallback_res

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
                        doctor_name = %s,
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
                        batch_id, visit_id, ward_name, bed_no, dept_id, dept_name, doctor_id, doctor_name, age,
                        ins_type, primary_icd, matched_rule_id, rule_version, current_fee,
                        target_base, fee_ratio, alert_level, pending_reason, discharge_review,
                        data_cutoff_time, synced_at
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, SYSDATETIME()
                    );
                """
                primary_icd_val = (diag_code or diag_name or '')[:50] or None
                params = (
                    # MATCHED params
                    batch_id, zyh,
                    ward_name, bed_no, dept_id, dept_name, doctor_id, doctor_name, age,
                    ins_type, primary_icd_val, matched_rule_id, self.rule_version,
                    current_fee, target_base, fee_ratio, alert_level, pending_reason,
                    discharge_review_flag, data_cutoff_time,
                    # NOT MATCHED params
                    batch_id, zyh, ward_name, bed_no, dept_id, dept_name, doctor_id, doctor_name, age,
                    ins_type, primary_icd_val, matched_rule_id, self.rule_version,
                    current_fee, target_base, fee_ratio, alert_level, pending_reason,
                    discharge_review_flag, data_cutoff_time
                )
                local_cur.execute(upsert_sql, params)

                evaluated_snapshots.append({
                    'visit_id': zyh,
                    'patient_name': pat.get('patient_name'),
                    'dept_name': dept_name,
                    'doctor_id': doctor_id,
                    'doctor_name': doctor_name,
                    'ins_type': ins_type,
                    'primary_icd': primary_icd_val,
                    'current_fee': float(current_fee),
                    'target_base': float(target_base) if target_base is not None else None,
                    'fee_ratio': float(fee_ratio) if fee_ratio is not None else None,
                    'alert_level': alert_level,
                    'pending_reason': pending_reason
                })

            batch_meta_sql = """
            MERGE dbo.inpatient_scan_batch AS target
            USING (SELECT %s AS batch_id) AS source
            ON (target.batch_id = source.batch_id)
            WHEN MATCHED THEN
                UPDATE SET
                    scope = %s,
                    year_month = %s,
                    rule_version = %s,
                    scan_limit = %s,
                    total_scanned = %s,
                    is_truncated = %s,
                    probe_version = %s,
                    data_cutoff_time = %s,
                    synced_at = SYSDATETIME()
            WHEN NOT MATCHED THEN
                INSERT (batch_id, scope, year_month, rule_version, scan_limit, total_scanned, is_truncated, probe_version, data_cutoff_time, synced_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, SYSDATETIME());
            """
            is_trunc_bit = 1 if month_meta.get('is_truncated') else 0
            ym_val = month_meta.get('year_month')
            lim_val = month_meta.get('scan_limit')
            tot_val = len(evaluated_snapshots)
            probe_ver_val = int(month_meta.get('probe_version') or 2)
            local_cur.execute(
                batch_meta_sql,
                (
                    batch_id, scope_val, ym_val, self.rule_version, lim_val, tot_val, is_trunc_bit, probe_ver_val, data_cutoff_time,
                    batch_id, scope_val, ym_val, self.rule_version, lim_val, tot_val, is_trunc_bit, probe_ver_val, data_cutoff_time,
                ),
            )
            local_conn.commit()
            print(f"[{batch_id}] Successfully persisted {len(evaluated_snapshots)} snapshot records to local_db.")
        except ValueError:
            try:
                local_conn.rollback()
            except Exception:
                pass
            raise
        except Exception as scan_err:
            # SNAP-LOG-001：使用受控错误码记录写入失败与真实回滚状态，严禁输出原始异常详情
            rollback_ok = False
            try:
                local_conn.rollback()
                rollback_ok = True
            except Exception:
                rollback_ok = False
            rb_state = "ROLLED_BACK" if rollback_ok else "ROLLBACK_FAILED"
            print(f"[{batch_id}] ERROR: EVENT=SCAN_BATCH_FAILED err_type={type(scan_err).__name__} rollback_status={rb_state}")
            raise RuntimeError(f"EVENT=SCAN_BATCH_FAILED batch={batch_id} rollback_status={rb_state}") from None
        finally:
            local_conn.close()

        dept_counts = {}
        for snap in evaluated_snapshots:
            lvl = snap.get('alert_level')
            if lvl in ('RED', 'YELLOW'):
                dname = snap.get('dept_name') or '未知科室'
                entry = dept_counts.setdefault(dname, {'RED': 0, 'YELLOW': 0})
                entry[lvl] += 1
        summary['dept_alert_counts'] = dept_counts
        summary['scope'] = scope_val
        summary['year_month'] = month_meta.get('year_month')
        summary['scan_limit'] = month_meta.get('scan_limit')
        summary['is_truncated'] = bool(month_meta.get('is_truncated'))
        summary['probe_version'] = int(month_meta.get('probe_version') or 2)
        summary['snapshots'] = evaluated_snapshots

        if notify_dingtalk:
            from tools.dingtalk_notifier import send_dingtalk_alert
            summary['dingtalk_notify'] = send_dingtalk_alert(summary)

        return summary

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Inpatient DIP Scanner")
    parser.add_argument("--batch-id", type=str, default=None, help="Custom batch ID")
    parser.add_argument("--dept", type=str, default=None, help="Filter by department")
    parser.add_argument("--limit", type=int, default=50, help="Scan limit")
    parser.add_argument("--settled", action="store_true", help="Include recently settled patients")
    parser.add_argument("--notify", action="store_true", help="Send DingTalk alert summary if RED/YELLOW alerts exist")
    args = parser.parse_args()

    scanner = InpatientScanner()
    res = scanner.scan_and_snapshot(
        batch_id=args.batch_id,
        department=args.dept,
        limit=args.limit,
        include_recent_settled=args.settled,
        notify_dingtalk=args.notify
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

# -*- coding: utf-8 -*-
"""
HIS Reader Module (Read-Only)
Connects to Baotou Yunkang TCM Hospital HIS Database (SQL Server 2012)
via FreeTDS alias 'hisdb' without forcing NOLOCK for settled reconciliation.
"""
import os
import sys
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Dict, Any, Optional
import pymssql
from dotenv import load_dotenv

# Ensure FreeTDS configuration path is available
if 'FREETDSCONF' not in os.environ:
    for candidate in [
        '/opt/hospital-agent/freetds.conf',
        '/home/yyserver/.freetds.conf',
        '/etc/freetds/freetds.conf'
    ]:
        if os.path.exists(candidate):
            os.environ['FREETDSCONF'] = candidate
            break

# Load environment variables
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env'))
load_dotenv('/opt/hospital-agent/.env')

class HISReader:
    def __init__(self):
        self.server = os.getenv('HIS_FREETDS_SECTION', 'hisdb')
        self.user = os.getenv('HIS_USER', 'agent_reader')
        self.password = os.getenv('HIS_PASS')
        if not self.password:
            raise ValueError("HIS_PASS environment variable must be explicitly configured and non-empty.")
        self.database = os.getenv('HIS_DB', 'his')
        self.charset = 'cp936'

    def get_connection(self):
        """Establish read-only connection to HIS SQL Server 2012."""
        return pymssql.connect(
            server=self.server,
            user=self.user,
            password=self.password,
            database=self.database,
            charset=self.charset
        )

    def get_settled_cases(self, limit: int = 50, department: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Fetch settled inpatient cases for audit & reconciliation.
        Joins his_zyjz with his_bl_sy and his_ZY_CYDJXX.
        Filters out canceled/voided settlements (iszf = 0).
        Does NOT force NOLOCK for accurate accounting audit.
        """
        conn = self.get_connection()
        cur = conn.cursor(as_dict=True)
        try:
            sql = """
            SELECT TOP (%s)
                jz.lsh,
                jz.id AS settlement_id,
                jz.amoney AS total_amount,
                jz.czmoney AS clinical_amount,
                jz.yjmoney AS tech_amount,
                jz.yfmoney AS pharmacy_amount,
                jz.yjjmoney AS deposit_amount,
                jz.ssmoney AS actual_invoiced,
                jz.jjzf AS fund_payment,
                jz.jzrq AS settlement_time,
                jz.iszf AS is_canceled,
                jz.isjz AS is_settled,
                sy.xm AS patient_name,
                sy.xb AS gender,
                sy.years AS age,
                sy.rykb AS admission_dept,
                sy.cykb AS discharge_dept,
                sy.rysj AS admission_time,
                sy.cysj AS discharge_time,
                sy.zyts AS stay_days,
                sy.zyysname AS attending_doctor,
                sy.kszrname AS dept_director,
                sy.ssbm1, sy.ssmc1,
                sy.ssbm2, sy.ssmc2,
                sy.ssbm3, sy.ssmc3,
                sy.ylzffs,
                cydj.CYZYZDDM AS primary_diag_code,
                cydj.CYZYZDMC AS primary_diag_name,
                cydj.GCYSBH AS doctor_code,
                jb.fylx AS insurance_type
            FROM dbo.his_zyjz jz
            LEFT JOIN dbo.his_bl_sy sy ON jz.lsh = sy.lsh
            LEFT JOIN dbo.his_ZY_CYDJXX cydj ON jz.lsh = cydj.ZYH
            LEFT JOIN dbo.jbxx jb ON jz.lsh = jb.zyh
            WHERE jz.iszf = 0 AND jz.amoney > 0
            """
            params = [limit]
            if department:
                sql += " AND (sy.cykb = %s OR sy.rykb = %s)"
                params.extend([department, department])

            sql += " ORDER BY jz.jzrq DESC"
            cur.execute(sql, tuple(params))
            return cur.fetchall()
        finally:
            conn.close()

    def get_case_fee_detail(self, lsh: str, settlement_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Retrieve complete fee breakdown for reconciliation:
        - his_zyjz master record (or aggregated admission-level settlements)
        - his_zycjz category amounts
        - his_zycz clinical items (sum & refund)
        - his_cfmain_fyjl pharmacy dispensing sum (sum & refund)
        - reconciliation_scope: 'SINGLE_SETTLEMENT' or 'ADMISSION_LEVEL'
        """
        conn = self.get_connection()
        cur = conn.cursor(as_dict=True)
        try:
            # 1. Query valid settlements for lsh
            cur.execute("SELECT * FROM dbo.his_zyjz WHERE lsh = %s AND iszf = 0 ORDER BY jzrq ASC", (lsh,))
            valid_settlements = cur.fetchall()
            if not valid_settlements:
                return {}

            # 严格校验传入的结算单号 (settlement_id)
            # 若调用方显式指定了单号，必须先核验该单号是否属于本次就诊流水且处于有效未作废状态
            # 若单号不存在、已作废或属于其他患者/住院记录，严禁返回假阳性对账数据，直接返回空字典
            if settlement_id is not None:
                target_str = str(settlement_id).strip()
                matched_target = [s for s in valid_settlements if str(s.get('id', '')).strip() == target_str]
                if not matched_target:
                    return {}

            # 判断结算单数量
            # 若同一次住院存在多张有效结算单，因明细表（his_zycz 与 his_cfmain_fyjl）在 HIS 架构中仅关联就诊流水号 lsh，
            # 无论调用方是否指定单张 settlement_id，均自动按整次住院汇总结算金额（ADMISSION_LEVEL）与整次住院明细对账，
            # 彻底杜绝“拿单张结算单金额比对整次住院明细”引发的范围不对齐
            if len(valid_settlements) > 1:
                reconciliation_scope = "ADMISSION_LEVEL"
                active_ids = [s['id'] for s in valid_settlements]
                total_amoney = sum((Decimal(str(s['amoney'])) for s in valid_settlements), Decimal('0'))
                total_czmoney = sum((Decimal(str(s.get('czmoney') or 0)) for s in valid_settlements), Decimal('0'))
                total_yjmoney = sum((Decimal(str(s.get('yjmoney') or 0)) for s in valid_settlements), Decimal('0'))
                total_yfmoney = sum((Decimal(str(s.get('yfmoney') or 0)) for s in valid_settlements), Decimal('0'))
                zyjz_record = {
                    'lsh': lsh,
                    'id': ','.join(str(i) for i in active_ids),
                    'target_settlement_id': str(settlement_id).strip() if settlement_id is not None else None,
                    'amoney': total_amoney,
                    'czmoney': total_czmoney,
                    'yjmoney': total_yjmoney,
                    'yfmoney': total_yfmoney,
                    'settlement_count': len(valid_settlements),
                    'multi_settlements': valid_settlements
                }
            else:
                reconciliation_scope = "SINGLE_SETTLEMENT"
                zyjz_record = valid_settlements[0]
                active_ids = [zyjz_record['id']]

            # 2. Category ledger (his_zycjz)
            if len(active_ids) == 1:
                cur.execute("""
                SELECT xmsflxid, amoney, ssmoney 
                FROM dbo.his_zycjz 
                WHERE lsh = %s AND id = %s
                """, (lsh, active_ids[0]))
            else:
                placeholders = ','.join(['%s'] * len(active_ids))
                cur.execute(f"""
                SELECT xmsflxid, SUM(amoney) as amoney, SUM(ssmoney) as ssmoney 
                FROM dbo.his_zycjz 
                WHERE lsh = %s AND id IN ({placeholders})
                GROUP BY xmsflxid
                """, [lsh] + active_ids)
            category_rows = cur.fetchall()
            category_sum = sum((Decimal(str(r['amoney'])) for r in category_rows), Decimal('0'))

            # 3. Clinical items (his_zycz)
            cur.execute("""
            SELECT 
                COUNT(*) as cz_count,
                SUM(money) as cz_total,
                SUM(CASE WHEN money < 0 THEN money ELSE 0 END) as cz_refund_total,
                SUM(CASE WHEN money > 0 THEN money ELSE 0 END) as cz_positive_total
            FROM dbo.his_zycz
            WHERE lsh = %s
            """, (lsh,))
            cz_stat = cur.fetchone()

            # 4. 药房对账（RECON-RX-001）：已发药明细（his_cfmain_fyjl）与
            # 已计费未发药处方（his_cfmain）必须在**同一条 SQL 语句**内聚合——
            # SQL Server 在 READ COMMITTED 下提供语句级一致快照，两组金额取自同一时点，
            # 两次独立查询之间发生发药时的“两边都不算”漏算窗口由此消除。
            # 未发药处方判据：已计费（jsrq 非空，未计费/弃用处方 jsrq 为空且本就不计入 yfmoney）、
            # 非作废、且无任何发药记录（his_cfmain_fyjl.id 与 his_cfmain.id 一一对应）；
            # isfy 标志在该 HIS 中维护不可靠，不作为判据
            cur.execute("""
            SELECT
                fy.fy_count           AS fy_count,
                fy.fy_total           AS fy_total,
                fy.fy_refund_total    AS fy_refund_total,
                fy.fy_positive_total  AS fy_positive_total,
                un.unfy_count         AS unfy_count,
                un.unfy_total         AS unfy_total
            FROM
                (SELECT
                    COUNT(*) AS fy_count,
                    SUM(bmoney) AS fy_total,
                    SUM(CASE WHEN bmoney < 0 THEN bmoney ELSE 0 END) AS fy_refund_total,
                    SUM(CASE WHEN bmoney > 0 THEN bmoney ELSE 0 END) AS fy_positive_total
                 FROM dbo.his_cfmain_fyjl
                 WHERE lsh = %s) fy
            CROSS JOIN
                (SELECT
                    COUNT(*) AS unfy_count,
                    SUM(c.bmoney) AS unfy_total
                 FROM dbo.his_cfmain c
                 WHERE c.lsh = %s
                   AND c.jsrq IS NOT NULL
                   AND (c.iszf = 0 OR c.iszf IS NULL)
                   AND NOT EXISTS (SELECT 1 FROM dbo.his_cfmain_fyjl f WHERE f.id = c.id)) un
            """, (lsh, lsh))
            fy_stat = cur.fetchone()
            unfy_stat = fy_stat  # 同一语句快照，两组金额天然同时点

            return {
                'reconciliation_scope': reconciliation_scope,
                'settlement_count': len(valid_settlements),
                'zyjz': zyjz_record,
                'categories': category_rows,
                'category_sum': category_sum,
                'cz_stat': {
                    'count': cz_stat['cz_count'],
                    'total': Decimal(str(cz_stat['cz_total'] or 0)),
                    'refund': Decimal(str(cz_stat['cz_refund_total'] or 0)),
                    'positive': Decimal(str(cz_stat['cz_positive_total'] or 0))
                },
                'fy_stat': {
                    'count': fy_stat['fy_count'],
                    'total': Decimal(str(fy_stat['fy_total'] or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
                    'refund': Decimal(str(fy_stat['fy_refund_total'] or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
                    'positive': Decimal(str(fy_stat['fy_positive_total'] or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
                    'undispensed_count': unfy_stat['unfy_count'],
                    'undispensed': Decimal(str(unfy_stat['unfy_total'] or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                }
            }
        finally:
            conn.close()

    def get_active_patient_fee(self, lsh: str, cutoff_time: Optional[str] = None) -> Dict[str, Any]:
        """
        Retrieve accumulated fees for an active inpatient who has not settled yet.
        Directly aggregates from his_zycz (clinical items) and his_cfmain_fyjl (pharmacy dispensing logs)
        up to cutoff_time (if provided) without querying his_zyjz.
        Distinguishes between verified activity, verified 0 fee, and query/data errors.
        """
        conn = self.get_connection()
        cur = conn.cursor(as_dict=True)
        try:
            # 1. Clinical items (his_zycz)
            sql_cz = """
            SELECT 
                COUNT(*) as cz_count,
                SUM(money) as cz_total,
                SUM(CASE WHEN money < 0 THEN money ELSE 0 END) as cz_refund_total,
                SUM(CASE WHEN money > 0 THEN money ELSE 0 END) as cz_positive_total
            FROM dbo.his_zycz
            WHERE lsh = %s
            """
            params_cz = [lsh]
            if cutoff_time:
                sql_cz += " AND ckrq <= %s"
                params_cz.append(cutoff_time)
            cur.execute(sql_cz, tuple(params_cz))
            cz_stat = cur.fetchone()

            # 2. Pharmacy dispensing logs (his_cfmain_fyjl)
            sql_fy = """
            SELECT 
                COUNT(*) as fy_count,
                SUM(bmoney) as fy_total,
                SUM(CASE WHEN bmoney < 0 THEN bmoney ELSE 0 END) as fy_refund_total,
                SUM(CASE WHEN bmoney > 0 THEN bmoney ELSE 0 END) as fy_positive_total
            FROM dbo.his_cfmain_fyjl
            WHERE lsh = %s
            """
            params_fy = [lsh]
            if cutoff_time:
                sql_fy += " AND fyrq <= %s"
                params_fy.append(cutoff_time)
            cur.execute(sql_fy, tuple(params_fy))
            fy_stat = cur.fetchone()

            cz_total = Decimal(str(cz_stat['cz_total'] or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            fy_total = Decimal(str(fy_stat['fy_total'] or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            total_fee = cz_total + fy_total

            cz_count = int(cz_stat['cz_count'] or 0)
            fy_count = int(fy_stat['fy_count'] or 0)
            has_activity = (cz_count > 0 or fy_count > 0)

            return {
                'lsh': lsh,
                'has_activity': has_activity,
                'cz_count': cz_count,
                'fy_count': fy_count,
                'cz_total': cz_total,
                'fy_total': fy_total,
                'total_fee': total_fee,
                'cz_stat': {
                    'count': cz_count,
                    'total': cz_total,
                    'refund': Decimal(str(cz_stat['cz_refund_total'] or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
                    'positive': Decimal(str(cz_stat['cz_positive_total'] or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                },
                'fy_stat': {
                    'count': fy_count,
                    'total': fy_total,
                    'refund': Decimal(str(fy_stat['fy_refund_total'] or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
                    'positive': Decimal(str(fy_stat['fy_positive_total'] or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                }
            }
        finally:
            conn.close()

    def get_patient_transfers(self, lsh: str) -> List[Dict[str, Any]]:
        """Query ward/bed transfers from his_zk."""
        conn = self.get_connection()
        cur = conn.cursor(as_dict=True)
        try:
            cur.execute("""
            SELECT lsh, id, czryid, sqrq, iszk, zkrq, yksid, ybfid, xksid, xbfid
            FROM dbo.his_zk
            WHERE lsh = %s
            ORDER BY sqrq ASC
            """, (lsh,))
            return cur.fetchall()
        finally:
            conn.close()

    def get_active_inpatients(self, department: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Fetch currently admitted patients (not yet discharged or settled).
        Identified where his_bl_sy.cysj IS NULL, or by active clinical fee activity without settlement.
        """
        conn = self.get_connection()
        cur = conn.cursor(as_dict=True)
        try:
            top_clause = f"TOP ({int(limit)})" if limit else ""
            sql = f"""
            SELECT DISTINCT {top_clause}
                sy.lsh AS zyh,
                sy.xm AS patient_name,
                sy.xb AS gender,
                sy.years AS age,
                COALESCE(zk_latest.xksid, sy.ksid) AS current_dept_id,
                COALESCE(zk_latest.xksname, sy.zkkb, sy.rykb) AS current_dept,
                COALESCE(zk_latest.xbfname, sy.rybf) AS current_bed,
                sy.rysj AS admission_time,
                sy.zyts AS current_stay_days,
                sy.zyysbm AS attending_doctor_id,
                sy.zyysname AS attending_doctor,
                sy.kszrname AS dept_director,
                sy.ryzdbm AS admission_diag_code,
                sy.ryzd AS admission_diag_name,
                sy.ssbm1, sy.ssmc1,
                sy.ssbm2, sy.ssmc2,
                sy.ssbm3, sy.ssmc3,
                sy.ylzffs,
                jb.fylx AS insurance_type
            FROM dbo.his_bl_sy sy
            LEFT JOIN dbo.jbxx jb ON sy.lsh = jb.zyh
            LEFT JOIN dbo.his_zyjz jz ON sy.lsh = jz.lsh AND jz.iszf = 0
            LEFT JOIN (
                SELECT zk.lsh, zk.xksid, ks.ksname AS xksname, bf.bfname AS xbfname,
                       ROW_NUMBER() OVER(PARTITION BY zk.lsh ORDER BY zk.sqrq DESC) as rn
                FROM dbo.his_zk zk
                LEFT JOIN dbo.his_ks ks ON zk.xksid = ks.ksid
                LEFT JOIN dbo.his_bf bf ON zk.xbfid = bf.bfid
                WHERE zk.iszk = 1
            ) zk_latest ON sy.lsh = zk_latest.lsh AND zk_latest.rn = 1
            WHERE sy.cysj IS NULL AND jz.lsh IS NULL
            """
            params = []
            if department:
                sql += " AND (COALESCE(zk_latest.xksname, sy.zkkb, sy.rykb) = %s OR COALESCE(zk_latest.xksname, sy.zkkb, sy.rykb) LIKE %s)"
                params.extend([department, f"%{department}%"])

            sql += " ORDER BY sy.rysj DESC"
            cur.execute(sql, tuple(params))
            return cur.fetchall()
        finally:
            conn.close()

    def get_patient_realtime_admission(self, lsh: str) -> Optional[Dict[str, Any]]:
        """
        实时穿透 HIS 查询患者当前在院信息（管床医生、最新转科换床与科室）。
        严禁使用 15 分钟快照授权，用于 DOCTOR 角色实时管床鉴权。
        """
        if not lsh:
            return None

        # 合成患者隔离支持：用于端到端与鉴权自动化回归，全程不读取真实患者隐私数据
        if str(lsh).startswith("SYNTH_"):
            from datetime import datetime
            if lsh in ("SYNTH_DOC_ATTEND", "SYNTH_GOLDEN_001"):
                return {
                    "zyh": lsh,
                    "patient_name": "合成患者-李鹏管床",
                    "gender": "男",
                    "age": "68岁",
                    "current_dept_id": "4",
                    "current_dept": "01中医科",
                    "current_bed": "01",
                    "admission_time": datetime(2026, 11, 24, 8, 0, 0),
                    "discharge_time": None,
                    "attending_doctor_id": "D150204006976",
                    "attending_doctor": "李鹏",
                    "dept_director_id": "depthead01",
                    "dept_director": "李主任",
                    "admission_diag_code": "M17.9",
                    "admission_diag_name": "未特指的膝关节病",
                    "ylzffs": "1",
                    "insurance_type": "职工"
                }
            elif lsh == "SYNTH_DOC_OTHER":
                return {
                    "zyh": lsh,
                    "patient_name": "合成患者-他管",
                    "gender": "女",
                    "age": "50岁",
                    "current_dept_id": "16",
                    "current_dept": "中医康复科",
                    "current_bed": "02",
                    "admission_time": datetime(2026, 11, 24, 8, 0, 0),
                    "discharge_time": None,
                    "attending_doctor_id": "DOC_OTHER_999",
                    "attending_doctor": "张三",
                    "dept_director_id": "depthead02",
                    "dept_director": "其他主任",
                    "admission_diag_code": "M17.9",
                    "admission_diag_name": "未特指的膝关节病",
                    "ylzffs": "1",
                    "insurance_type": "居民"
                }
            elif lsh == "SYNTH_DOC_MISSING":
                return {
                    "zyh": lsh,
                    "patient_name": "合成患者-缺管床",
                    "gender": "男",
                    "age": "60岁",
                    "current_dept_id": "4",
                    "current_dept": "01中医科",
                    "current_bed": "03",
                    "admission_time": datetime(2026, 11, 24, 8, 0, 0),
                    "discharge_time": None,
                    "attending_doctor_id": None,
                    "attending_doctor": None,
                    "dept_director_id": "depthead01",
                    "dept_director": "李主任",
                    "admission_diag_code": "M17.9",
                    "admission_diag_name": "未特指的膝关节病",
                    "ylzffs": "1",
                    "insurance_type": "职工"
                }
            elif lsh == "SYNTH_DISCHARGED":
                # 已出院患者：在院核验返回 None
                return None
            elif lsh == "SYNTH_DEPT_DIFF":
                # 跨科室患者：科室为中医康复科 (id=16)，管床医生为 D150204006976
                return {
                    "zyh": lsh,
                    "patient_name": "合成患者-跨科",
                    "gender": "男",
                    "age": "65岁",
                    "current_dept_id": "16",
                    "current_dept": "中医康复科",
                    "current_bed": "05",
                    "admission_time": datetime(2026, 11, 24, 8, 0, 0),
                    "discharge_time": None,
                    "attending_doctor_id": "D150204006976",
                    "attending_doctor": "李鹏",
                    "dept_director_id": "depthead02",
                    "dept_director": "其他主任",
                    "admission_diag_code": "M17.9",
                    "admission_diag_name": "未特指的膝关节病",
                    "ylzffs": "1",
                    "insurance_type": "职工"
                }

        conn = self.get_connection()
        cur = conn.cursor(as_dict=True)
        try:
            sql = """
            SELECT TOP 1
                sy.lsh AS zyh,
                sy.xm AS patient_name,
                sy.xb AS gender,
                sy.years AS age,
                COALESCE(zk_latest.xksid, sy.ksid) AS current_dept_id,
                COALESCE(zk_latest.xksname, sy.zkkb, sy.rykb) AS current_dept,
                COALESCE(zk_latest.xbfname, sy.rybf) AS current_bed,
                sy.rysj AS admission_time,
                sy.cysj AS discharge_time,
                sy.zyysbm AS attending_doctor_id,
                sy.zyysname AS attending_doctor,
                sy.kszrbm AS dept_director_id,
                sy.kszrname AS dept_director,
                sy.ryzdbm AS admission_diag_code,
                sy.ryzd AS admission_diag_name,
                sy.ylzffs,
                jb.fylx AS insurance_type
            FROM dbo.his_bl_sy sy
            LEFT JOIN dbo.jbxx jb ON sy.lsh = jb.zyh
            LEFT JOIN dbo.his_zyjz jz ON sy.lsh = jz.lsh AND jz.iszf = 0
            LEFT JOIN (
                SELECT zk.lsh, zk.xksid, ks.ksname AS xksname, bf.bfname AS xbfname,
                       ROW_NUMBER() OVER(PARTITION BY zk.lsh ORDER BY zk.sqrq DESC) as rn
                FROM dbo.his_zk zk
                LEFT JOIN dbo.his_ks ks ON zk.xksid = ks.ksid
                LEFT JOIN dbo.his_bf bf ON zk.xbfid = bf.bfid
                WHERE zk.iszk = 1
            ) zk_latest ON sy.lsh = zk_latest.lsh AND zk_latest.rn = 1
            WHERE sy.lsh = %s AND sy.cysj IS NULL AND jz.lsh IS NULL
            ORDER BY sy.rysj DESC
            """
            cur.execute(sql, (lsh,))
            return cur.fetchone()
        finally:
            conn.close()

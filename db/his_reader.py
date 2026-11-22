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
        - his_zyjz master record
        - his_zycjz category amounts
        - his_zycz clinical items (sum & refund)
        - his_cfmain_fyjl pharmacy dispensing sum (sum & refund)
        """
        conn = self.get_connection()
        cur = conn.cursor(as_dict=True)
        try:
            # 1. his_zyjz master
            if settlement_id:
                cur.execute("SELECT * FROM dbo.his_zyjz WHERE lsh = %s AND id = %s", (lsh, settlement_id))
            else:
                cur.execute("SELECT TOP 1 * FROM dbo.his_zyjz WHERE lsh = %s AND iszf = 0 ORDER BY jzrq DESC", (lsh,))
            zyjz_record = cur.fetchone()
            if not zyjz_record:
                return {}

            active_id = zyjz_record['id']

            # 2. Category ledger (his_zycjz)
            cur.execute("""
            SELECT xmsflxid, amoney, ssmoney 
            FROM dbo.his_zycjz 
            WHERE lsh = %s AND id = %s
            """, (lsh, active_id))
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

            # 4. Pharmacy dispensing logs (his_cfmain_fyjl)
            cur.execute("""
            SELECT 
                COUNT(*) as fy_count,
                SUM(bmoney) as fy_total,
                SUM(CASE WHEN bmoney < 0 THEN bmoney ELSE 0 END) as fy_refund_total,
                SUM(CASE WHEN bmoney > 0 THEN bmoney ELSE 0 END) as fy_positive_total
            FROM dbo.his_cfmain_fyjl
            WHERE lsh = %s
            """, (lsh,))
            fy_stat = cur.fetchone()

            return {
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
                    'positive': Decimal(str(fy_stat['fy_positive_total'] or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
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
                sy.rykb AS current_dept,
                sy.rybf AS current_bed,
                sy.rysj AS admission_time,
                sy.zyts AS current_stay_days,
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
            WHERE sy.cysj IS NULL AND jz.lsh IS NULL
            """
            params = []
            if department:
                sql += " AND sy.rykb = %s"
                params.append(department)

            sql += " ORDER BY sy.rysj DESC"
            cur.execute(sql, tuple(params))
            return cur.fetchall()
        finally:
            conn.close()

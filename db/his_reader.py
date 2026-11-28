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
    # RECON-RX-001：对账读取的一致性隔离级别。生产环境严禁修改，恒为 SERIALIZABLE；
    # 仅验收测试的变异子类可将其覆盖为 None（跳过显式隔离设置，退回数据库默认
    # READ COMMITTED），用于证明一致性验收对隔离保护确实敏感（变异必须失败）。
    RECON_ISOLATION = "SERIALIZABLE"

    def __init__(self):
        self.server = os.getenv('HIS_FREETDS_SECTION', 'hisdb')
        self.user = os.getenv('HIS_USER', 'agent_reader')
        self.password = os.getenv('HIS_PASS')
        if not self.password:
            raise ValueError("HIS_PASS environment variable must be explicitly configured and non-empty.")
        self.database = os.getenv('HIS_DB', 'his')
        self.charset = 'cp936'
        self.login_timeout = int(os.getenv('HIS_LOGIN_TIMEOUT', '60'))

    def get_connection(self):
        """Establish read-only connection to HIS SQL Server 2012."""
        return pymssql.connect(
            server=self.server,
            user=self.user,
            password=self.password,
            database=self.database,
            charset=self.charset,
            login_timeout=self.login_timeout,
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
            # RECON-RX-001：主单（his_zyjz）、类目账、临嘱与药费聚合必须在同一受保证的
            # 一致性边界内读取。生产 HIS 库实测 snapshot_isolation_state=OFF 且未开启 RCSI，
            # 不能依赖行版本快照，普通 READ COMMITTED 下独立语句仍可能取自不同时点；
            # 这里显式采用 SERIALIZABLE 只读事务（键范围锁仅作用于本 lsh 的少量行；
            # 本函数仅供对账审计低频调用，无 API 在线路径），
            # 保证并发结算/发药提交时对账只读到完整旧账或完整新账。
            conn.autocommit(False)
            if self.RECON_ISOLATION:
                cur.execute("SET TRANSACTION ISOLATION LEVEL " + self.RECON_ISOLATION)
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
            # 已计费未发药处方（his_cfmain）在同一条 SQL 语句内聚合，
            # 且与主单读取同处上文建立的 SERIALIZABLE 显式事务，
            # 两组金额与对账基准天然对应同一时点，无读间漏算窗口。
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

            result = {
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
            conn.commit()  # 结束 SERIALIZABLE 只读事务，及时释放键范围锁
            return result
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
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

    # AUTH-ATTEND-001：拆分权威管床授权医生 (_AUTH_ATTENDING_DOC_SELECT) 与看板展示用推断医生 (_ATTENDING_DOC_SELECT)：
    # 1) 权威来源仅采信唯一且在册的标准医生主键 ryid (his_ry.isys=1)：
    #    - 病案首页 his_bl_sy.zyysbm 同时合并 ybysdm 与 gjgbm_ybysdm 候选集后统一判重 (HAVING COUNT(DISTINCT ryid) = 1)；
    #      若跨列冲突或未映射到在册医生，权威字段严格返回 NULL，原始编码仅保留给看板展示。
    #    - 若 sy.zyysbm 为空，仅当 his_gh (ysid / gdfysid / zzysid) 对应唯一在册医生 r_gh.ryid IS NOT NULL 时采信。
    # 2) order_doc 仅按开单频次做看板展示兜底 (attending_doctor_source='ORDER_INFERRED')，绝不写入 auth_attending_doctor_id，
    #    也不在实时穿透鉴权接口 get_patient_realtime_admission 中返回或作为授权依据。
    _AUTH_ATTENDING_DOC_ID_EXPR = """CASE
                    WHEN NULLIF(LTRIM(RTRIM(sy.zyysbm)), '') IS NOT NULL
                        THEN CAST(ry_sy.ryid AS VARCHAR(32))
                    WHEN r_gh.ryid IS NOT NULL
                        THEN CAST(r_gh.ryid AS VARCHAR(32))
                    ELSE NULL
                END"""

    _AUTH_ATTENDING_DOC_SELECT = f"""
                {_AUTH_ATTENDING_DOC_ID_EXPR} AS auth_attending_doctor_id,
                {_AUTH_ATTENDING_DOC_ID_EXPR} AS attending_doctor_id,
                CASE
                    WHEN NULLIF(LTRIM(RTRIM(sy.zyysbm)), '') IS NOT NULL AND ry_sy.ryid IS NOT NULL THEN 'SY_ZYYSBM'
                    WHEN NULLIF(LTRIM(RTRIM(sy.zyysbm)), '') IS NULL AND r_gh.ryid IS NOT NULL THEN 'HIS_GH'
                    ELSE NULL
                END AS attending_doctor_source,
                COALESCE(
                    NULLIF(LTRIM(RTRIM(sy.zyysname)), ''),
                    NULLIF(LTRIM(RTRIM(ry_sy.ryname)), ''),
                    CASE
                        WHEN NULLIF(LTRIM(RTRIM(sy.zyysbm)), '') IS NULL
                            THEN COALESCE(
                                NULLIF(LTRIM(RTRIM(r_gh.ryname)), ''),
                                NULLIF(LTRIM(RTRIM(gh.ysname)), ''),
                                NULLIF(LTRIM(RTRIM(gh.zzysname)), '')
                            )
                    END
                ) AS attending_doctor,"""

    _ATTENDING_DOC_SELECT = f"""
                {_AUTH_ATTENDING_DOC_ID_EXPR} AS auth_attending_doctor_id,
                COALESCE(
                    {_AUTH_ATTENDING_DOC_ID_EXPR},
                    NULLIF(LTRIM(RTRIM(sy.zyysbm)), ''),
                    CAST(COALESCE(NULLIF(gh.ysid, 0), NULLIF(gh.gdfysid, 0), NULLIF(gh.zzysid, 0)) AS VARCHAR(32)),
                    CAST(order_doc.ryid AS VARCHAR(32))
                ) AS attending_doctor_id,
                CASE
                    WHEN NULLIF(LTRIM(RTRIM(sy.zyysbm)), '') IS NOT NULL AND ry_sy.ryid IS NOT NULL THEN 'SY_ZYYSBM'
                    WHEN NULLIF(LTRIM(RTRIM(sy.zyysbm)), '') IS NOT NULL THEN 'SY_ZYYSBM_RAW'
                    WHEN r_gh.ryid IS NOT NULL OR COALESCE(NULLIF(gh.ysid, 0), NULLIF(gh.gdfysid, 0), NULLIF(gh.zzysid, 0)) IS NOT NULL THEN 'HIS_GH'
                    WHEN order_doc.ryid IS NOT NULL THEN 'ORDER_INFERRED'
                    ELSE NULL
                END AS attending_doctor_source,
                COALESCE(
                    NULLIF(LTRIM(RTRIM(sy.zyysname)), ''),
                    NULLIF(LTRIM(RTRIM(ry_sy.ryname)), ''),
                    CASE
                        WHEN NULLIF(LTRIM(RTRIM(sy.zyysbm)), '') IS NULL
                            THEN COALESCE(
                                NULLIF(LTRIM(RTRIM(r_gh.ryname)), ''),
                                NULLIF(LTRIM(RTRIM(gh.ysname)), ''),
                                NULLIF(LTRIM(RTRIM(gh.zzysname)), ''),
                                order_doc.ryname
                            )
                    END
                ) AS attending_doctor,"""

    # 共享科室与床位解析 SQL 片段：
    # 1) 转科优先 (zk_latest.xksid / xksname)，否则取住院登记主表 his_gh (ksid 关联 his_ks -> ksname) 与 his_gh.cwhname；
    # 2) his_bl_sy 中空白占位符 '-' 视同 NULL，防止 sy.zkkb='-' 阻断兜底导致展示 '-' 与 dept_id 缺失。
    _DEPT_EXPR = """COALESCE(
                    NULLIF(NULLIF(LTRIM(RTRIM(zk_latest.xksname)), ''), '-'),
                    NULLIF(NULLIF(LTRIM(RTRIM(ks_gh.ksname)), ''), '-'),
                    NULLIF(NULLIF(LTRIM(RTRIM(gh.ksname)), ''), '-'),
                    NULLIF(NULLIF(LTRIM(RTRIM(sy.zkkb)), ''), '-'),
                    NULLIF(NULLIF(LTRIM(RTRIM(sy.rykb)), ''), '-')
                )"""

    _DEPT_BED_SELECT = f"""
                COALESCE(zk_latest.xksid, NULLIF(gh.ksid, 0), NULLIF(sy.ksid, 0)) AS current_dept_id,
                {_DEPT_EXPR} AS current_dept,
                COALESCE(
                    NULLIF(NULLIF(LTRIM(RTRIM(zk_latest.xbfname)), ''), '-'),
                    NULLIF(NULLIF(LTRIM(RTRIM(gh.cwhname)), ''), '-'),
                    NULLIF(NULLIF(LTRIM(RTRIM(sy.rybf)), ''), '-')
                ) AS current_bed,"""

    _CLINICAL_DIAG_SELECT = """
                COALESCE(
                    NULLIF(NULLIF(LTRIM(RTRIM(sy.ryzdbm)), ''), '-'),
                    NULLIF(NULLIF(LTRIM(RTRIM(gh.jbbm)), ''), '-')
                ) AS admission_diag_code,
                COALESCE(
                    NULLIF(NULLIF(LTRIM(RTRIM(sy.ryzd)), ''), '-'),
                    NULLIF(NULLIF(LTRIM(RTRIM(gh.jbname)), ''), '-'),
                    NULLIF(NULLIF(LTRIM(RTRIM(gh.lczd)), ''), '-')
                ) AS admission_diag_name,"""

    # 共享参保类型解析 SQL 片段：
    # 1) 优先取 explicit 覆盖字段 jb.fylx；
    # 2) 自费(jylxid=2)与公费(jylxid=3)严禁被医保卡历史险种误识别为职工/居民；
    # 3) 国家医保 1101 实时有效参保表 his_btyb_rycbxx_1101 (310 职工 / 390 居民) 修正 his_gh.xzlxid 滞后缓存；
    # 4) 兜底按 his_gh.jylxid=1(居民) / 4(城镇医保按 xzlxid 消歧，缺失保持歧义)。
    _INSURANCE_TYPE_SELECT = """
                COALESCE(
                    NULLIF(LTRIM(RTRIM(jb.fylx)), ''),
                    CASE
                        WHEN gh.jylxid = 2 OR LTRIM(RTRIM(ISNULL(gh.jylxname, ''))) = '自费' THEN N'自费'
                        WHEN gh.jylxid = 3 OR LTRIM(RTRIM(ISNULL(gh.jylxname, ''))) = '公费' THEN N'公费医疗'
                        WHEN yb1101.insutype = '310' THEN N'职工基本医疗保险'
                        WHEN yb1101.insutype = '390' THEN N'城乡居民基本医疗保险'
                        WHEN gh.jylxid = 1 THEN N'居民'
                        WHEN gh.jylxid = 4 THEN
                            CASE
                                WHEN LTRIM(RTRIM(ISNULL(gh.xzlxid, ''))) = '310' THEN N'职工基本医疗保险'
                                WHEN LTRIM(RTRIM(ISNULL(gh.xzlxid, ''))) = '390' THEN N'城乡居民基本医疗保险'
                                ELSE NULLIF(LTRIM(RTRIM(gh.jylxname)), '')
                            END
                        ELSE COALESCE(NULLIF(LTRIM(RTRIM(gh.xzlxname)), ''), NULLIF(LTRIM(RTRIM(gh.jylxname)), ''))
                    END
                ) AS insurance_type"""

    _AUTH_ATTENDING_DOC_JOINS = """
            LEFT JOIN (
                SELECT
                    code_key,
                    MAX(ryid) AS ryid,
                    MAX(ryname) AS ryname
                FROM (
                    SELECT LTRIM(RTRIM(ybysdm)) AS code_key, ryid, LTRIM(RTRIM(ISNULL(ryname, ''))) AS ryname
                    FROM dbo.his_ry
                    WHERE isys = 1 AND NULLIF(LTRIM(RTRIM(ybysdm)), '') IS NOT NULL
                    UNION ALL
                    SELECT LTRIM(RTRIM(gjgbm_ybysdm)) AS code_key, ryid, LTRIM(RTRIM(ISNULL(ryname, ''))) AS ryname
                    FROM dbo.his_ry
                    WHERE isys = 1 AND NULLIF(LTRIM(RTRIM(gjgbm_ybysdm)), '') IS NOT NULL
                ) sy_doc_map
                GROUP BY code_key
                HAVING COUNT(DISTINCT ryid) = 1
            ) ry_sy ON LTRIM(RTRIM(sy.zyysbm)) = ry_sy.code_key
            OUTER APPLY (
                SELECT TOP 1
                    g.ysid, g.gdfysid, g.zzysid, g.ysname, g.zzysname,
                    g.ksid, g.ksname, g.cwhname,
                    g.jylxid, g.jylxname, g.xzlxid, g.xzlxname, g.psn_no,
                    g.jbbm, g.jbname, g.lczd, g.ssbm, g.ssname, g.oldyear, g.zyts, g.ghrq
                FROM dbo.his_gh g
                WHERE g.lsh = sy.lsh
            ) gh
            LEFT JOIN dbo.his_ks ks_gh ON gh.ksid = ks_gh.ksid
            OUTER APPLY (
                SELECT TOP 1 LTRIM(RTRIM(r1101.insutype)) AS insutype
                FROM dbo.his_btyb_rycbxx_1101 r1101
                WHERE NULLIF(LTRIM(RTRIM(gh.psn_no)), '') IS NOT NULL
                  AND r1101.psn_no = gh.psn_no
                  AND r1101.psn_insu_stas = '1'
                ORDER BY r1101.psn_insu_date DESC
            ) yb1101
            LEFT JOIN dbo.his_ry r_gh
                ON COALESCE(NULLIF(gh.ysid, 0), NULLIF(gh.gdfysid, 0), NULLIF(gh.zzysid, 0)) = r_gh.ryid
               AND r_gh.isys = 1"""

    _ATTENDING_DOC_JOINS = _AUTH_ATTENDING_DOC_JOINS + """
            OUTER APPLY (
                SELECT TOP 1 r.ryid, LTRIM(RTRIM(ISNULL(r.ryname, ''))) AS ryname
                FROM (
                    SELECT cz.ysid AS ysid, cz.ckrq AS act_ts
                    FROM dbo.his_zycz cz
                    WHERE cz.lsh = sy.lsh AND cz.ysid IS NOT NULL AND ISNULL(cz.istf, 0) = 0
                    UNION ALL
                    SELECT yz.cfysid AS ysid, CAST(NULL AS DATETIME) AS act_ts
                    FROM dbo.his_bl_yzzycf yz
                    WHERE yz.lsh = sy.lsh AND yz.cfysid IS NOT NULL
                    UNION ALL
                    SELECT cf.ysid AS ysid, cf.cfrq AS act_ts
                    FROM dbo.his_zycfmain cf
                    WHERE cf.lsh = sy.lsh AND cf.ysid IS NOT NULL AND ISNULL(cf.istf, 0) = 0
                ) ord
                INNER JOIN dbo.his_ry r ON ord.ysid = r.ryid AND r.isys = 1
                GROUP BY r.ryid, LTRIM(RTRIM(ISNULL(r.ryname, '')))
                ORDER BY COUNT(*) DESC, MAX(ord.act_ts) DESC, r.ryid DESC
            ) order_doc"""

    def get_his_doctors(self, include_non_doctors: bool = False) -> List[Dict[str, Any]]:
        """
        从 HIS 职员字典表 dbo.his_ry（关联 dbo.his_ks）只读获取医生目录（脱敏，绝不查询身份证号、手机号、口令）。
        以唯一主键 ryid 作为标准 doctor_id。
        """
        conn = self.get_connection()
        cur = conn.cursor(as_dict=True)
        try:
            where_clause = "" if include_non_doctors else "WHERE r.isys = 1"
            sql = f"""
            SELECT
                CAST(r.ryid AS VARCHAR(32)) AS doctor_id,
                r.ryid AS ryid,
                LTRIM(RTRIM(ISNULL(r.ryname, ''))) AS doctor_name,
                LTRIM(RTRIM(ISNULL(r.zh, ''))) AS zh,
                LTRIM(RTRIM(ISNULL(r.ybysdm, ''))) AS ybysdm,
                LTRIM(RTRIM(ISNULL(r.gjgbm_ybysdm, ''))) AS gjgbm_ybysdm,
                CASE WHEN r.ksid IS NOT NULL THEN CAST(r.ksid AS VARCHAR(32)) ELSE '' END AS dept_id,
                LTRIM(RTRIM(ISNULL(ks.ksname, ''))) AS dept_name,
                CAST(ISNULL(r.isys, 0) AS INT) AS is_doctor
            FROM dbo.his_ry r
            LEFT JOIN dbo.his_ks ks ON r.ksid = ks.ksid
            {where_clause}
            ORDER BY r.ksid ASC, r.ryid ASC
            """
            cur.execute(sql)
            return cur.fetchall()
        finally:
            conn.close()

    def get_active_inpatients(
        self,
        department: Optional[str] = None,
        limit: Optional[int] = None,
        return_meta: bool = False,
    ) -> Any:
        """
        Fetch currently admitted patients (not yet discharged or settled).
        Identified where his_bl_sy.cysj IS NULL, or by active clinical fee activity without settlement.
        When limit is set, probes limit+1 rows so callers know accurately whether truncation occurred.
        """
        conn = self.get_connection()
        cur = conn.cursor(as_dict=True)
        try:
            probe_limit = (int(limit) + 1) if limit is not None else None
            top_clause = f"TOP ({probe_limit})" if probe_limit else ""
            sql = f"""
            SELECT DISTINCT {top_clause}
                sy.lsh AS zyh,
                sy.xm AS patient_name,
                sy.xb AS gender,
                COALESCE(NULLIF(LTRIM(RTRIM(CAST(sy.years AS VARCHAR(20)))), ''), CAST(CAST(ROUND(gh.oldyear, 0) AS INT) AS VARCHAR(20))) AS age,
                {self._DEPT_BED_SELECT}
                sy.rysj AS admission_time,
                COALESCE(
                    NULLIF(NULLIF(LTRIM(RTRIM(CAST(sy.zyts AS VARCHAR(20)))), ''), '0'),
                    CAST(NULLIF(gh.zyts, 0) AS VARCHAR(20)),
                    CAST(CASE WHEN DATEDIFF(DAY, COALESCE(sy.rysj, gh.ghrq), GETDATE()) >= 1 THEN DATEDIFF(DAY, COALESCE(sy.rysj, gh.ghrq), GETDATE()) ELSE 1 END AS VARCHAR(20))
                ) AS current_stay_days,
                {self._ATTENDING_DOC_SELECT}
                sy.kszrname AS dept_director,
                {self._CLINICAL_DIAG_SELECT}
                COALESCE(NULLIF(NULLIF(LTRIM(RTRIM(sy.ssbm1)), ''), '-'), NULLIF(NULLIF(LTRIM(RTRIM(gh.ssbm)), ''), '-')) AS ssbm1,
                COALESCE(NULLIF(NULLIF(LTRIM(RTRIM(sy.ssmc1)), ''), '-'), NULLIF(NULLIF(LTRIM(RTRIM(gh.ssname)), ''), '-')) AS ssmc1,
                sy.ssbm2, sy.ssmc2,
                sy.ssbm3, sy.ssmc3,
                sy.ylzffs,
                {self._INSURANCE_TYPE_SELECT}
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
            {self._ATTENDING_DOC_JOINS}
            WHERE sy.cysj IS NULL AND jz.lsh IS NULL
            """
            params = []
            if department:
                sql += f" AND ({self._DEPT_EXPR} = %s OR {self._DEPT_EXPR} LIKE %s)"
                params.extend([department, f"%{department}%"])

            sql += " ORDER BY sy.rysj DESC"
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
            is_truncated = False
            if limit is not None and len(rows) > int(limit):
                rows = rows[:int(limit)]
                is_truncated = True
            if return_meta:
                return rows, {
                    "scan_limit": int(limit) if limit is not None else None,
                    "total_scanned": len(rows),
                    "is_truncated": is_truncated,
                }
            return rows
        finally:
            conn.close()

    def get_active_billed_procedures(self, lsh: str) -> List[str]:
        """
        从在院医嘱/处置明细 (dbo.his_zycz) 提取净数量 > 0（自动剔除退费冲红）的实际已计费操作项目名称。
        用于在院期病案首页尚未编码手术操作时辅助识别已实施的专科检查/操作。
        """
        if not lsh:
            return []
        conn = self.get_connection()
        cur = conn.cursor(as_dict=True)
        try:
            cur.execute(
                """
                SELECT xmname
                FROM dbo.his_zycz WITH (NOLOCK)
                WHERE lsh = %s AND ISNULL(istf, 0) = 0
                GROUP BY xmname
                HAVING SUM(ISNULL(sl, 0)) > 0
                ORDER BY xmname
                """,
                (lsh,),
            )
            return [str(r["xmname"]).strip() for r in cur.fetchall() if r.get("xmname")]
        finally:
            conn.close()

    def get_monthly_inpatients(
        self,
        year_month: Optional[str] = None,
        department: Optional[str] = None,
        limit: Optional[int] = None,
        page_size: int = 200,
        return_meta: bool = False,
    ) -> Any:
        """
        MONTH-SCOPE-001 & MONTH-TOTAL-001：
        查询与目标月份（默认当月）存在住院区间重叠的全部住院患者（含跨月持续在院及当月出院/结算患者）。
        - 区间重叠判定：入院时间 < 下月初，且（仍在院未结 OR 出院/最近结算时间 >= 本月初）；
        - limit=None 时按稳定排序分页全量拉取并按 zyh 去重，不静默截断；
        - 若显式传入 limit，探测是否存在更多记录并返回 is_truncated 标记。
        """
        import re as _re
        from datetime import datetime, timezone, timedelta
        bj_now = datetime.now(timezone(timedelta(hours=8)))
        ym = (year_month or bj_now.strftime("%Y-%m")).strip()
        m_ym = _re.match(r'^(\d{4})-(0[1-9]|1[0-2])$', ym)
        if not m_ym:
            raise ValueError(f"year_month 格式非法（期望 YYYY-MM）: {ym}")
        year_i, month_i = int(m_ym.group(1)), int(m_ym.group(2))
        start_str = f"{year_i:04d}-{month_i:02d}-01"
        if month_i == 12:
            end_str = f"{year_i + 1:04d}-01-01"
        else:
            end_str = f"{year_i:04d}-{month_i + 1:02d}-01"

        conn = self.get_connection()
        cur = conn.cursor(as_dict=True)
        try:
            base_sql = f"""
            SELECT
                sy.lsh AS zyh,
                sy.xm AS patient_name,
                sy.xb AS gender,
                COALESCE(NULLIF(LTRIM(RTRIM(CAST(sy.years AS VARCHAR(20)))), ''), CAST(CAST(ROUND(gh.oldyear, 0) AS INT) AS VARCHAR(20))) AS age,
                COALESCE(zk_latest.xksid, NULLIF(gh.ksid, 0), NULLIF(sy.ksid, 0)) AS current_dept_id,
                {self._DEPT_EXPR} AS current_dept,
                CASE
                    WHEN jz_sum.lsh IS NOT NULL THEN N'已结算'
                    WHEN sy.cysj IS NOT NULL THEN N'已出院'
                    ELSE COALESCE(
                        NULLIF(NULLIF(LTRIM(RTRIM(zk_latest.xbfname)), ''), '-'),
                        NULLIF(NULLIF(LTRIM(RTRIM(gh.cwhname)), ''), '-'),
                        NULLIF(NULLIF(LTRIM(RTRIM(sy.rybf)), ''), '-')
                    )
                END AS current_bed,
                sy.rysj AS admission_time,
                sy.cysj AS discharge_time,
                COALESCE(
                    NULLIF(NULLIF(LTRIM(RTRIM(CAST(sy.zyts AS VARCHAR(20)))), ''), '0'),
                    CAST(NULLIF(gh.zyts, 0) AS VARCHAR(20)),
                    CAST(CASE WHEN DATEDIFF(DAY, COALESCE(sy.rysj, gh.ghrq), COALESCE(sy.cysj, GETDATE())) >= 1
                              THEN DATEDIFF(DAY, COALESCE(sy.rysj, gh.ghrq), COALESCE(sy.cysj, GETDATE()))
                              ELSE 1 END AS VARCHAR(20))
                ) AS current_stay_days,
                {self._ATTENDING_DOC_SELECT}
                sy.kszrname AS dept_director,
                COALESCE(
                    NULLIF(NULLIF(LTRIM(RTRIM(cydj.CYZYZDDM)), ''), '-'),
                    NULLIF(NULLIF(LTRIM(RTRIM(sy.xycyzd1bm)), ''), '-'),
                    NULLIF(NULLIF(LTRIM(RTRIM(sy.ryzdbm)), ''), '-'),
                    NULLIF(NULLIF(LTRIM(RTRIM(gh.jbbm)), ''), '-')
                ) AS admission_diag_code,
                COALESCE(
                    NULLIF(NULLIF(LTRIM(RTRIM(cydj.CYZYZDMC)), ''), '-'),
                    NULLIF(NULLIF(LTRIM(RTRIM(sy.xycyzd1)), ''), '-'),
                    NULLIF(NULLIF(LTRIM(RTRIM(sy.ryzd)), ''), '-'),
                    NULLIF(NULLIF(LTRIM(RTRIM(gh.jbname)), ''), '-'),
                    NULLIF(NULLIF(LTRIM(RTRIM(gh.lczd)), ''), '-')
                ) AS admission_diag_name,
                COALESCE(NULLIF(NULLIF(LTRIM(RTRIM(sy.ssbm1)), ''), '-'), NULLIF(NULLIF(LTRIM(RTRIM(gh.ssbm)), ''), '-')) AS ssbm1,
                COALESCE(NULLIF(NULLIF(LTRIM(RTRIM(sy.ssmc1)), ''), '-'), NULLIF(NULLIF(LTRIM(RTRIM(gh.ssname)), ''), '-')) AS ssmc1,
                NULLIF(NULLIF(LTRIM(RTRIM(sy.ssbm2)), ''), '-') AS ssbm2,
                NULLIF(NULLIF(LTRIM(RTRIM(sy.ssmc2)), ''), '-') AS ssmc2,
                NULLIF(NULLIF(LTRIM(RTRIM(sy.ssbm3)), ''), '-') AS ssbm3,
                NULLIF(NULLIF(LTRIM(RTRIM(sy.ssmc3)), ''), '-') AS ssmc3,
                sy.ylzffs,
                {self._INSURANCE_TYPE_SELECT},
                jz_sum.total_amount AS settled_total_amount,
                CASE WHEN jz_sum.lsh IS NOT NULL THEN 1 ELSE 0 END AS is_settled
            FROM dbo.his_bl_sy sy
            LEFT JOIN (
                SELECT lsh, SUM(amoney) AS total_amount, MAX(jzrq) AS latest_jzrq
                FROM dbo.his_zyjz
                WHERE iszf = 0 AND amoney > 0
                GROUP BY lsh
            ) jz_sum ON sy.lsh = jz_sum.lsh
            LEFT JOIN dbo.his_ZY_CYDJXX cydj ON sy.lsh = cydj.ZYH
            LEFT JOIN dbo.jbxx jb ON sy.lsh = jb.zyh
            LEFT JOIN (
                SELECT zk.lsh, zk.xksid, ks.ksname AS xksname, bf.bfname AS xbfname,
                       ROW_NUMBER() OVER(PARTITION BY zk.lsh ORDER BY zk.sqrq DESC) as rn
                FROM dbo.his_zk zk
                LEFT JOIN dbo.his_ks ks ON zk.xksid = ks.ksid
                LEFT JOIN dbo.his_bf bf ON zk.xbfid = bf.bfid
                WHERE zk.iszk = 1
            ) zk_latest ON sy.lsh = zk_latest.lsh AND zk_latest.rn = 1
            {self._ATTENDING_DOC_JOINS}
            WHERE COALESCE(sy.rysj, gh.ghrq) < %s
              AND (
                  (sy.cysj IS NULL AND jz_sum.latest_jzrq IS NULL)
                  OR COALESCE(sy.cysj, jz_sum.latest_jzrq) >= %s
              )
            """
            base_params: List[Any] = [end_str, start_str]
            if department:
                base_sql += f" AND ({self._DEPT_EXPR} = %s OR {self._DEPT_EXPR} LIKE %s)"
                base_params.extend([department, f"%{department}%"])
            base_sql += " ORDER BY COALESCE(sy.rysj, gh.ghrq) DESC, sy.lsh ASC OFFSET %s ROWS FETCH NEXT %s ROWS ONLY"

            deduped: List[Dict[str, Any]] = []
            seen_zyh = set()
            is_truncated = False
            chunk_size = max(1, int(page_size or 200))
            offset = 0

            while True:
                if limit is not None and len(deduped) >= int(limit):
                    fetch_count = chunk_size
                elif limit is not None:
                    remaining = int(limit) - len(deduped)
                    fetch_count = min(chunk_size, remaining + 1)
                else:
                    fetch_count = chunk_size

                cur.execute(base_sql, tuple(base_params + [offset, fetch_count]))
                batch = cur.fetchall()
                if not batch:
                    break

                for r in batch:
                    zyh_val = r.get("zyh")
                    if zyh_val and zyh_val in seen_zyh:
                        continue
                    if limit is not None and len(deduped) >= int(limit):
                        is_truncated = True
                        break
                    if zyh_val:
                        seen_zyh.add(zyh_val)
                    if r.get("is_settled") and r.get("settled_total_amount") is not None:
                        r["_pre_settled_amount"] = r["settled_total_amount"]
                        r["_is_settled"] = True
                    deduped.append(r)

                if is_truncated or len(batch) < fetch_count:
                    break
                offset += len(batch)

            if return_meta:
                return deduped, {
                    "year_month": ym,
                    "total_scanned": len(deduped),
                    "scan_limit": int(limit) if limit is not None else None,
                    "is_truncated": is_truncated,
                }
            return deduped
        finally:
            conn.close()

    def get_patient_realtime_admission(self, lsh: str) -> Optional[Dict[str, Any]]:
        """
        实时穿透 HIS 查询患者当前在院信息（管床医生、最新转科换床与科室）。
        严禁使用 15 分钟快照授权及开单频次推断医生，严格用于 DOCTOR / DEPT_HEAD 实时鉴权。
        """
        if not lsh:
            return None

        conn = self.get_connection()
        cur = conn.cursor(as_dict=True)
        try:
            sql = f"""
            SELECT TOP 1
                sy.lsh AS zyh,
                sy.xm AS patient_name,
                sy.xb AS gender,
                COALESCE(NULLIF(LTRIM(RTRIM(CAST(sy.years AS VARCHAR(20)))), ''), CAST(CAST(ROUND(gh.oldyear, 0) AS INT) AS VARCHAR(20))) AS age,
                {self._DEPT_BED_SELECT}
                sy.rysj AS admission_time,
                sy.cysj AS discharge_time,
                {self._AUTH_ATTENDING_DOC_SELECT}
                sy.kszrbm AS dept_director_id,
                sy.kszrname AS dept_director,
                {self._CLINICAL_DIAG_SELECT}
                sy.ylzffs,
                {self._INSURANCE_TYPE_SELECT}
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
            {self._AUTH_ATTENDING_DOC_JOINS}
            WHERE sy.lsh = %s AND sy.cysj IS NULL AND jz.lsh IS NULL
            ORDER BY sy.rysj DESC
            """
            cur.execute(sql, (lsh,))
            return cur.fetchone()
        finally:
            conn.close()

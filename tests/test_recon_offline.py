# -*- coding: utf-8 -*-
"""
离线对账回归（RECON-TEST-001 收尾，tests/test_recon_offline.py）
可独立运行、无需 HIS 凭据、无需网络、无需 LOCAL_DB_*。
捕获 get_case_fee_detail 实际下发的药房合并聚合 SQL，在 SQLite 内存库原样执行：
合成已发药/退款/已计费未发药/未计费/作废五类记录分别断言；
删除作废过滤条件的变异查询必须得出不同结果（杜绝“桩在 Python 重算过滤条件”失真）。
与 tests/test_settled_reconciliation.py Suite 8 同源，供无数据库环境单独回归。
编号 RX-* 均为虚构合成编号，非真实就诊流水号。
"""
import os
import sys
import sqlite3
from decimal import Decimal

# 加入项目根路径
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from db.his_reader import HISReader


class RecCursor:
    """记录型游标：捕获读取器实际下发的 SQL，主单/类目/临嘱答合成数据"""
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
            return {'cz_count': 0, 'cz_total': Decimal('0'),
                    'cz_refund_total': Decimal('0'), 'cz_positive_total': Decimal('0')}
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
    """绕过 __init__ 的 HIS_PASS 校验，仅供离线捕获 SQL 之用"""
    def __init__(self):
        self.conn_obj = RecConn()

    def get_connection(self):
        return self.conn_obj


def run_recon_offline():
    print("=" * 70)
    print("  RECON OFFLINE SUITE: actual pharmacy SQL on SQLite + mutation")
    print("=" * 70)

    rec_reader = RecReader()
    rec_reader.get_case_fee_detail('zy_synth_sql')
    fy_sql_list = [s for s in rec_reader.conn_obj.cur.sqls if 'unfy_count' in s]
    assert len(fy_sql_list) == 1, "药房聚合应为一条合并 SQL（语句内同时点）"
    fy_sql = fy_sql_list[0]

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
    print("PASS [Offline-a]: 实际 SQL 在 SQLite 原样执行——发药净额 25.00（含退款 -5.00）、未发药 1 笔 12.00，未计费/作废正确排除")

    # 变异对照：删除作废过滤条件，结果必须改变（证明断言确实依赖该过滤）
    mutated_sql = adapted_sql.replace("AND (c.iszf = 0 OR c.iszf IS NULL)", "AND (1 = 1)")
    assert mutated_sql != adapted_sql, "变异未生效"
    lc.execute(mutated_sql, ('zy_synth_sql', 'zy_synth_sql'))
    mrow = lc.fetchone()
    assert abs(mrow[5] - 67.00) < 1e-9 and abs(mrow[5] - row[5]) > 1e-9, "删除作废过滤的变异查询必须失败（计入作废 55.00）"
    lite.close()
    print("PASS [Offline-b]: 变异对照——删除作废过滤条件后未发药变为 67.00，原查询断言对该变异敏感")

    print("\n" + "=" * 70)
    print("  RECON OFFLINE SUITE PASSED (100.0%)")
    print("=" * 70)


if __name__ == '__main__':
    run_recon_offline()

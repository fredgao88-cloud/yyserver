# -*- coding: utf-8 -*-
"""
DIP 规则样表导入脚本
严格遵循《开发设计文档 v1.2》架构规范：
1. 内存先行校验：先完整解析各工作表有效数据，杜绝 Excel 尾部空白格式行遍历；
2. 单一事务原子性：解析校验通过后，在同一数据库事务内执行版本清理与批量插入，失败自动回滚；
3. 统一封装连接：通过 db.local_db 读取配置，严禁硬编码账密。
"""
import os
import sys
import openpyxl
from decimal import Decimal
from typing import Optional, List, Dict, Any, Tuple

# 确保能加载项目内部模块
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from db.local_db import get_local_db_connection

DEFAULT_EXCEL_PATH = os.getenv("DIP_EXCEL_PATH", "/opt/hospital-agent/data/dip_rules.xlsx")

def find_excel_file(path_hint: Optional[str] = None) -> str:
    candidates = []
    if path_hint:
        candidates.append(path_hint)
    candidates.extend([
        DEFAULT_EXCEL_PATH,
        "/tmp/dip_rules.xlsx",
        os.path.join(os.path.dirname(__file__), "..", "data", "dip_rules.xlsx"),
        os.path.join(os.path.dirname(__file__), "..", "data", "医保病种分值.xlsx")
    ])
    for p in candidates:
        if p and os.path.exists(p):
            return os.path.abspath(p)
    raise FileNotFoundError(f"未找到 DIP 规则 Excel 文件，请检查以下路径: {candidates}")

def parse_excel_to_memory(real_path: str, rule_version: str) -> Tuple[List[tuple], Dict[str, int]]:
    """在内存中先行解析并校验 Excel 数据，遇到有效数据结束即停止，避免遍历百万空白行"""
    print(f"[*] 正在读取并校验 Excel 文件: {real_path} ...")
    wb = openpyxl.load_workbook(real_path, data_only=True, read_only=True)
    
    configs = [
        ("中医", "中医科"),
        ("消化", "消化内科")
    ]
    
    parsed_rows: List[tuple] = []
    stats: Dict[str, int] = {}
    
    source_filename = os.path.basename(real_path)

    for sname, dept_name in configs:
        if sname not in wb.sheetnames:
            print(f"[!] 工作表 {sname} 不存在，跳过")
            continue
            
        sheet = wb[sname]
        dept_count = 0
        empty_streak = 0
        
        # 使用 iter_rows 逐行读取，遇到连续空行立即退出当前表
        for r in sheet.iter_rows(min_row=2, values_only=True):
            if not r or not any(r):
                empty_streak += 1
                if empty_streak >= 5:
                    break
                continue
                
            group_name = r[0]
            if group_name is None or not str(group_name).strip():
                empty_streak += 1
                if empty_streak >= 5:
                    break
                continue
            
            empty_streak = 0
            group_name_str = str(group_name).strip()
            icd_code = str(r[1]).strip() if r[1] is not None else ""
            icd_name = str(r[2]).strip() if r[2] is not None else ""
            op_code = str(r[3]).strip() if r[3] is not None else ""
            op_name = str(r[4]).strip() if r[4] is not None else ""
            catalog_type = str(r[5]).strip() if r[5] is not None else "1"
            
            aux_flag = 0
            if r[6] is not None:
                try:
                    aux_flag = int(float(str(r[6]).strip()))
                except ValueError:
                    aux_flag = 0
                    
            aux_detail = str(r[7]).strip() if r[7] is not None else ""
            billing_unit = "PER_BED_DAY" if ("按床日" in aux_detail or "按床日" in group_name_str) else "PER_CASE"
            
            try:
                dip_score = Decimal(str(r[8]))
                resident_base = Decimal(str(r[9]))
                resident_min = Decimal(str(r[10]))
                resident_max = Decimal(str(r[11]))
                employee_base = Decimal(str(r[12]))
                employee_min = Decimal(str(r[13]))
                employee_max = Decimal(str(r[14]))
            except Exception as ex:
                raise ValueError(f"行数据数值转换异常 [{dept_name} - {group_name_str}]: {ex}")

            parsed_rows.append((
                "LOCAL_SAMPLE",
                rule_version,
                "2026-01-01",
                None,
                source_filename,
                "APPROVED",
                dept_name,
                group_name_str,
                icd_code,
                icd_name,
                op_code,
                op_name,
                catalog_type,
                aux_flag,
                aux_detail,
                billing_unit,
                dip_score,
                Decimal("7.0000"),
                Decimal("7.5000"),
                Decimal("0.6000"),
                Decimal("0.9000"),
                Decimal("1.1000"),
                "HALF_UP_2DP",
                resident_base,
                resident_min,
                resident_max,
                employee_base,
                employee_min,
                employee_max
            ))
            dept_count += 1
            
        stats[dept_name] = dept_count
        print(f"[+] 内存解析完成: 科室 [{dept_name}] 共 {dept_count} 条有效规则")
        
    wb.close()
    if not parsed_rows:
        raise ValueError("Excel 中未解析到任何有效 DIP 规则，终止导入以保护现有数据库")
        
    return parsed_rows, stats

def import_rules(excel_path: Optional[str] = None, rule_version: str = "2026_SAMPLE_V1"):
    real_path = find_excel_file(excel_path)
    
    # 步骤 1: 内存先行解析与校验
    parsed_rows, stats = parse_excel_to_memory(real_path, rule_version)
    print(f"[*] 内存校验通过，准备以单一原子事务入库: 共 {len(parsed_rows)} 条记录 ...")
    
    # 步骤 2: 单一事务原子替换入库
    cursor, conn = get_local_db_connection(as_dict=False)
    try:
        # 清理已有该版本历史数据（事务内）
        cursor.execute("DELETE FROM dbo.dip_rules WHERE rule_version = %s", (rule_version,))
        
        insert_sql = """
        INSERT INTO dbo.dip_rules (
            region_code, rule_version, effective_date, expiry_date, source_doc,
            review_status, department, dip_group_name, icd_code, icd_name,
            op_code, op_name, catalog_type, aux_flag, aux_detail,
            billing_unit, dip_score, resident_point, employee_point, hosp_coef,
            low_threshold, high_threshold, rounding_mode,
            resident_base, resident_min, resident_max,
            employee_base, employee_min, employee_max
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s
        )
        """
        cursor.executemany(insert_sql, parsed_rows)
        
        # 整体原子提交
        conn.commit()
        print(f"[SUCCESS] 样表规则原子导入成功！版本号: {rule_version}，总条数: {len(parsed_rows)}，分布: {stats}")
        return len(parsed_rows)
    except Exception as e:
        conn.rollback()
        print(f"[ERROR] 规则导入失败，事务已自动回滚，数据库未受损: {e}")
        raise
    finally:
        conn.close()

if __name__ == "__main__":
    hint = sys.argv[1] if len(sys.argv) > 1 else None
    import_rules(hint)

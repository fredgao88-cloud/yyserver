# -*- coding: utf-8 -*-
"""
DIP 规则测算与判灯核心引擎模块
严格按照《开发设计文档 v1.2》架构与准则实现：
1. 严禁硬编码任何病种分值或点值系数（如 7.5*0.6 / 7.0*0.6），必须读取 dbo.dip_rules 生效版本参数；
2. 严禁使用浮点数，全局使用 Decimal 高精度未舍入交叉相乘判灯；
3. 判定标准：<90% 绿灯，90%~110% (含端点) 黄灯，>110% 红灯，临界浮动（如 110.004%）标注“略高于 110%”；
4. 自费患者明确标注 NOT_APPLICABLE；
5. 多候选/缺参/按床日缺天数明确标注 PENDING 并返回原因，绝不强行盲取第一条规则。
"""
import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Dict, Any, List
from db.local_db import query_approved_dip_rules

# 内存缓存，避免高并发下单条查询频繁打满数据库，支持主动刷新
_RULES_CACHE: Dict[str, List[Dict[str, Any]]] = {}

def get_loaded_rules(rule_version: str = "2026_SAMPLE_V1", force_reload: bool = False) -> List[Dict[str, Any]]:
    """获取指定生效版本的 DIP 规则集（动态从数据库加载）"""
    global _RULES_CACHE
    if force_reload or rule_version not in _RULES_CACHE:
        rules = query_approved_dip_rules(rule_version=rule_version)
        _RULES_CACHE[rule_version] = rules
    return _RULES_CACHE[rule_version]

def normalize_insurance_type(ins_type_str: Optional[str]) -> Dict[str, Any]:
    """
    统一参保类型解析与合规映射：
    - 返回标准类别：'职工'、'居民'、'自费'
    - '城镇居民'、'城乡居民'、'居民医保'、'新农合' -> '居民'（绝不因包含'城镇'而误入职工）
    - '城镇职工'、'职工医保'、'职工' -> '职工'
    - 仅有'城镇'而未指明'职工'或'居民' -> PENDING: AMBIGUOUS_INS_TYPE
    - '公费医疗'、'工伤'等统筹区尚未核准映射的险种 -> PENDING: UNSUPPORTED_INS_TYPE
    - 缺失/None/空 -> PENDING: MISSING_INS_TYPE
    """
    if ins_type_str is None:
        return {
            "valid": False,
            "category": None,
            "status": "PENDING",
            "reason": "MISSING_INS_TYPE",
            "message": "缺少参保类型（职工/居民/自费），无法确定对应点值与测算基准"
        }
    s = str(ins_type_str).strip()
    if not s or s.upper() in ("NONE", "NULL", "UNKNOWN", "未填", "未知"):
        return {
            "valid": False,
            "category": None,
            "status": "PENDING",
            "reason": "MISSING_INS_TYPE",
            "message": "缺少有效参保类型（职工/居民/自费），无法确定对应点值与测算基准"
        }

    if "自费" in s:
        return {
            "valid": True,
            "category": "自费",
            "status": "NOT_APPLICABLE",
            "reason": "SELF_PAY",
            "message": "自费患者不适用医保 DIP 支付规则"
        }

    # 优先匹配公费医疗与工伤保险等非基本医保统筹险种（即使包含'职工'或'退休'，也绝不落入职工分支）
    if "公费" in s:
        return {
            "valid": False,
            "category": None,
            "status": "PENDING",
            "reason": "UNSUPPORTED_INS_TYPE:公费医疗",
            "message": "公费医疗统筹区尚未核准 DIP 映射，不适用基本医保 DIP 测算基准，需人工核实"
        }
    if "工伤" in s:
        return {
            "valid": False,
            "category": None,
            "status": "PENDING",
            "reason": "UNSUPPORTED_INS_TYPE:工伤保险",
            "message": "工伤保险由工伤保险基金单独支付，不适用基本医保 DIP 分值付费测算"
        }

    # 优先匹配“城镇居民”与“居民”系列，绝不因包含“城镇”而错落入职工
    if any(k in s for k in ("城镇居民", "居民", "城乡", "农合", "新农合")):
        return {
            "valid": True,
            "category": "居民",
            "status": "MATCHED",
            "reason": None,
            "message": None
        }

    # 匹配职工系列
    if any(k in s for k in ("城镇职工", "职工", "在职职工", "退休职工")):
        return {
            "valid": True,
            "category": "职工",
            "status": "MATCHED",
            "reason": None,
            "message": None
        }

    # 仅有职业状态（如退休、在职、离休）而未指明具体医保险种
    if any(k in s for k in ("退休", "在职", "离休", "无业", "待业", "务农", "学生")):
        return {
            "valid": False,
            "category": None,
            "status": "PENDING",
            "reason": "OCCUPATION_NOT_INSURANCE_TYPE",
            "message": f"【{s}】属于职业状态而非医保参保类型，请明确为城镇职工医保、城乡居民医保、公费医疗或自费"
        }

    # 仅有“城镇”字样但无“职工”或“居民”
    if "城镇" in s:
        return {
            "valid": False,
            "category": None,
            "status": "PENDING",
            "reason": "AMBIGUOUS_INS_TYPE",
            "message": f"参保类型【{s}】含义模糊（需明确为城镇职工还是城镇居民），两者统筹点值不同，需明确后测算"
        }

    # 公费医疗、工伤等统筹区尚未核准映射的类型
    return {
        "valid": False,
        "category": None,
        "status": "PENDING",
        "reason": f"UNSUPPORTED_INS_TYPE:{s}",
        "message": f"参保类型【{s}】不适用当前统筹区基本医保 DIP 测算基准（仅支持职工/居民），需人工核实，绝不盲目套用居民标准"
    }

def evaluate_traffic_light(
    current_cost: Decimal,
    benchmark: Decimal,
    low_threshold: Decimal = Decimal("0.9000"),
    high_threshold: Decimal = Decimal("1.1000"),
    ins_type: str = "职工"
) -> Dict[str, Any]:
    """
    高精度判灯评估核心函数：
    1. 自费患者返回 NOT_APPLICABLE；
    2. 使用 Decimal 未舍入交叉相乘比较判灯：
       cost < benchmark * low_threshold -> 绿灯
       benchmark * low_threshold <= cost <= benchmark * high_threshold -> 黄灯
       cost > benchmark * high_threshold -> 红灯
    3. 临界浮动判定（如 110.004% 标注“略高于 110%”，89.99% 标注“略低于 90%”）
    """
    if "自费" in ins_type:
        return {
            "light": "无",
            "status": "NOT_APPLICABLE",
            "message": "自费患者不适用医保 DIP 支付规则",
            "ratio_percent": None,
            "boundary_note": None,
            "lower_bound": None,
            "upper_bound": None
        }

    if benchmark <= Decimal("0"):
        return {
            "light": "灰灯",
            "status": "INVALID_BENCHMARK",
            "message": "基准金额非正数，无法测算",
            "ratio_percent": None,
            "boundary_note": None,
            "lower_bound": None,
            "upper_bound": None
        }

    # 十进制交叉相乘比较（未舍入判断端点）
    cost_val = current_cost
    lower_limit = benchmark * low_threshold
    upper_limit = benchmark * high_threshold

    # 精确比率计算（仅用于展示）
    ratio = (cost_val / benchmark) * Decimal("100")
    ratio_float = float(ratio.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
    ratio_display = float(ratio.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    # 端点与区间判定
    boundary_note = None
    if cost_val < lower_limit:
        light = "绿灯"
        status = "费用正常（<90%）"
        # 临界判定：89.5% <= ratio < 90%
        if ratio >= Decimal("89.5"):
            boundary_note = "略低于 90%"
    elif cost_val <= upper_limit:
        light = "黄灯"
        status = "预警观察（90%~110%）"
    else:
        light = "红灯"
        status = "费用超标（>110%）"
        # 临界判定：110% < ratio <= 110.5% (例如 110.004%)
        if ratio <= Decimal("110.5"):
            boundary_note = "略高于 110%"

    return {
        "light": light,
        "status": status,
        "ratio_percent": ratio_display,
        "ratio_exact": ratio_float,
        "boundary_note": boundary_note,
        "lower_bound": float(lower_limit.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
        "upper_bound": float(upper_limit.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    }

def match_dip_rule(
    disease: str,
    age: Optional[int] = None,
    ins_type: Optional[str] = None,
    op_keyword: Optional[str] = None,
    bed_days: Optional[int] = None,
    is_tcm_advantage: Optional[bool] = None,
    rule_version: str = "2026_SAMPLE_V1"
) -> Dict[str, Any]:
    """
    DIP 规则匹配主流程：
    1. 统一参保类型校验（职工/居民/自费，严格区分城镇居民与城镇职工） -> PENDING (MISSING_INS_TYPE / UNSUPPORTED_INS_TYPE)
    2. 自费拦截 -> NOT_APPLICABLE
    3. 从库中读取生效规则进行检索匹配
    4. 多重属性过滤（ICD编码/诊断名称、年龄辅助目录、手术/操作类型、床日付费标识）
    5. 针对多候选、缺参、按床日缺天数严格标 PENDING，绝不盲取第一条
    6. 读取 matched rule 自身参数计算基准金额，零基准/无效基准统一拦截为 PENDING (INVALID_BENCHMARK)
    """
    norm_ins = normalize_insurance_type(ins_type)
    if not norm_ins["valid"]:
        return {
            "matched": False,
            "status": "PENDING",
            "pending_reason": norm_ins["reason"],
            "message": norm_ins["message"]
        }
    ins_is_self_pay = (norm_ins["category"] == "自费")
    is_employee = (norm_ins["category"] == "职工")
    is_resident = (norm_ins["category"] == "居民")
    
    # 获取数据库生效规则
    try:
        all_rules = get_loaded_rules(rule_version=rule_version)
    except Exception as e:
        return {
            "matched": False,
            "status": "DB_ERROR",
            "message": f"加载 DIP 规则库失败: {str(e)}",
            "rule_version": rule_version
        }

    if not all_rules:
        return {
            "matched": False,
            "status": "NO_RULES",
            "message": f"规则库中未找到生效版本为 {rule_version} 的已审核规则",
            "rule_version": rule_version
        }

    # Step 1: 基础疾病诊断/ICD 模糊与精确匹配
    kw = disease.strip().upper()
    # 清理末尾可能的占位符，如 R51.x -> R51
    kw_clean = re.sub(r'\.[Xx]$', '', kw)

    matched_candidates: List[Dict[str, Any]] = []
    for r in all_rules:
        r_icd = (r["icd_code"] or "").strip().upper()
        r_icd_clean = re.sub(r'\.[Xx]$', '', r_icd)
        r_icd_name = (r["icd_name"] or "").strip().upper()
        r_group_name = (r["dip_group_name"] or "").strip().upper()
        
        # 匹配 ICD 编码（精确或前缀）或疾病名称匹配
        if (kw == r_icd) or (kw_clean and kw_clean == r_icd_clean) or (kw in r_icd_name) or (kw in r_group_name):
            matched_candidates.append(r)

    if not matched_candidates:
        return {
            "matched": False,
            "status": "NOT_FOUND",
            "message": f"在规则库({rule_version})中未匹配到相关病种：{disease}",
            "disease_query": disease,
            "rule_version": rule_version
        }

    # Step 1.5: 宽泛病名且未指定治疗方案检查
    # 若仅输入中文病名且未指定手术/保守方案，绝不擅自假设为保守治疗并盲目套用单一金额
    has_explicit_code = bool(re.search(r'\b[A-Z]\d{2}', kw))
    if not has_explicit_code and op_keyword is None and len(matched_candidates) > 1:
        return {
            "matched": False,
            "status": "PENDING",
            "pending_reason": "AMBIGUOUS_BROAD_DIAGNOSIS",
            "message": f"“{disease}”属于范围较宽的临床诊断，未提供具体治疗方案（保守治疗 / 手术操作）或明确 ICD 编码，需明确后测算，绝不盲目套用第一条",
            "candidates": [
                {
                    "rule_id": c["rule_id"],
                    "dip_group_name": c["dip_group_name"],
                    "op_name": c["op_name"],
                    "aux_detail": c["aux_detail"],
                    "dip_score": float(c["dip_score"])
                } for c in matched_candidates[:5]
            ]
        }

    # Step 2: 年龄辅助目录过滤
    # 样表中存在形如 “年龄分类(年龄辅助目录-66岁及以上)”
    has_age_specific_rule = any("66岁及以上" in (r["aux_detail"] or r["dip_group_name"]) for r in matched_candidates)
    if has_age_specific_rule:
        if age is None:
            # 存在高龄区分组，但未提供患者年龄，无法唯一确定
            return {
                "matched": False,
                "status": "PENDING",
                "pending_reason": "MISSING_AGE",
                "message": f"该病种存在高龄组（66岁及以上）与普通组区分，缺少年龄参数，无法确定分组，绝不盲目套用第一条",
                "candidates": [
                    {
                        "rule_id": c["rule_id"],
                        "dip_group_name": c["dip_group_name"],
                        "aux_detail": c["aux_detail"],
                        "dip_score": float(c["dip_score"])
                    } for c in matched_candidates
                ]
            }
        elif age >= 66:
            # 筛选出高龄组
            high_age_group = [c for c in matched_candidates if "66岁及以上" in (c["aux_detail"] or c["dip_group_name"])]
            if high_age_group:
                matched_candidates = high_age_group
        else:
            # 年龄小于66岁，剔除高龄专属组
            matched_candidates = [c for c in matched_candidates if "66岁及以上" not in (c["aux_detail"] or c["dip_group_name"])]

    # Step 3: 手术/操作类型过滤
    if op_keyword:
        op_kw = op_keyword.strip()
        filtered_by_op = []
        for c in matched_candidates:
            op_name = c["op_name"] or ""
            op_code = c["op_code"] or ""
            grp_name = c["dip_group_name"] or ""
            if (op_kw in op_name) or (op_kw in op_code) or (op_kw in grp_name):
                filtered_by_op.append(c)
        if filtered_by_op:
            matched_candidates = filtered_by_op
    else:
        # 如果未指定手术操作，但候选规则中同时包含“保守治疗”和“手术/内镜/治疗性操作”，必须判定为 PENDING，不得盲取
        has_conservative = any("保守" in (c["op_name"] or c["dip_group_name"]) for c in matched_candidates)
        has_procedure = any(
            ("切除" in (c["op_name"] or c["dip_group_name"])) or 
            ("操作" in (c["op_name"] or c["dip_group_name"])) or
            ("镜" in (c["op_name"] or c["dip_group_name"]))
            for c in matched_candidates
        )
        if has_conservative and has_procedure and len(matched_candidates) > 1:
            return {
                "matched": False,
                "status": "PENDING",
                "pending_reason": "MISSING_OPERATION",
                "message": f"该病种包含保守治疗与多种手术/操作分组候选，需补充拟行或已执行操作，绝不盲目套用第一条",
                "candidates": [
                    {
                        "rule_id": c["rule_id"],
                        "dip_group_name": c["dip_group_name"],
                        "op_name": c["op_name"],
                        "billing_unit": c["billing_unit"],
                        "dip_score": float(c["dip_score"])
                    } for c in matched_candidates
                ]
            }

    # Step 4: 计费单位过滤（PER_CASE 与 PER_BED_DAY）
    # 比如颈臂综合征 M53.1 有按床日付费（康复类）与常规按病种付费
    if len(matched_candidates) > 1:
        has_bed_day = any(c["billing_unit"] == "PER_BED_DAY" for c in matched_candidates)
        has_case = any(c["billing_unit"] == "PER_CASE" for c in matched_candidates)
        if has_bed_day and has_case:
            if op_keyword and ("床日" in op_keyword or "康复" in op_keyword):
                matched_candidates = [c for c in matched_candidates if c["billing_unit"] == "PER_BED_DAY"]
            elif op_keyword and ("按病种" in op_keyword or "常规" in op_keyword):
                matched_candidates = [c for c in matched_candidates if c["billing_unit"] == "PER_CASE"]
            else:
                return {
                    "matched": False,
                    "status": "PENDING",
                    "pending_reason": "AMBIGUOUS_BILLING_UNIT",
                    "message": "该病种同时支持按病种付费(PER_CASE)与康复按床日付费(PER_BED_DAY)，需明确是否按床日康复结算",
                    "candidates": [
                        {
                            "rule_id": c["rule_id"],
                            "dip_group_name": c["dip_group_name"],
                            "billing_unit": c["billing_unit"],
                            "dip_score": float(c["dip_score"])
                        } for c in matched_candidates
                    ]
                }

    # Step 4.5: 中医优势病种过滤与区分
    if len(matched_candidates) > 1:
        has_tcm = any("中医优势" in (c["aux_detail"] or c["dip_group_name"]) for c in matched_candidates)
        has_non_tcm = any("中医优势" not in (c["aux_detail"] or c["dip_group_name"]) for c in matched_candidates)
        if has_tcm and has_non_tcm:
            if is_tcm_advantage is True or (op_keyword and "中医优势" in op_keyword):
                matched_candidates = [c for c in matched_candidates if "中医优势" in (c["aux_detail"] or c["dip_group_name"])]
            elif is_tcm_advantage is False or (op_keyword and ("常规" in op_keyword or "非中医优势" in op_keyword or "普通" in op_keyword)):
                matched_candidates = [c for c in matched_candidates if "中医优势" not in (c["aux_detail"] or c["dip_group_name"])]
            else:
                return {
                    "matched": False,
                    "status": "PENDING",
                    "pending_reason": "AMBIGUOUS_TCM_ADVANTAGE",
                    "message": "该病种存在常规保守组与中医优势病种组区分，需明确是否属于中医优势病种，绝不盲目套用第一条",
                    "candidates": [
                        {
                            "rule_id": c["rule_id"],
                            "dip_group_name": c["dip_group_name"],
                            "billing_unit": c["billing_unit"],
                            "dip_score": float(c["dip_score"])
                        } for c in matched_candidates
                    ]
                }

    # Step 5: 候选判定
    if len(matched_candidates) > 1:
        return {
            "matched": False,
            "status": "PENDING",
            "pending_reason": "MULTIPLE_CANDIDATES",
            "message": f"存在 {len(matched_candidates)} 条候选分组，需进一步明确诊断/年龄/手术信息，绝不强行取第一条",
            "candidates": [
                {
                    "rule_id": c["rule_id"],
                    "dip_group_name": c["dip_group_name"],
                    "op_name": c["op_name"],
                    "aux_detail": c["aux_detail"],
                    "billing_unit": c["billing_unit"],
                    "dip_score": float(c["dip_score"])
                } for c in matched_candidates
            ]
        }
        
    if len(matched_candidates) == 0:
        return {
            "matched": False,
            "status": "NOT_FOUND",
            "message": "根据所提供的限制条件（年龄/手术）未筛选到匹配的规则",
            "rule_version": rule_version
        }

    # 唯一定位到规则
    rule = matched_candidates[0]

    # Step 6: 检查按床日付费的在院天数要求
    is_bed_day = (rule["billing_unit"] == "PER_BED_DAY")
    if is_bed_day and (bed_days is None or bed_days <= 0):
        return {
            "matched": False,
            "status": "PENDING",
            "pending_reason": "MISSING_BED_DAYS",
            "message": "该病种为按床日付费（PER_BED_DAY），缺少有效在院天数参数，无法计算基准",
            "target_rule": {
                "rule_id": rule["rule_id"],
                "dip_group_name": rule["dip_group_name"],
                "billing_unit": rule["billing_unit"],
                "dip_score": float(rule["dip_score"])
            }
        }

    # Step 7: 自费逻辑拦截
    if ins_is_self_pay:
        return {
            "matched": True,
            "status": "NOT_APPLICABLE",
            "message": "自费患者不适用医保 DIP 支付规则",
            "rule_id": rule["rule_id"],
            "rule_version": rule["rule_version"],
            "disease_code": rule["icd_code"],
            "disease_name": rule["icd_name"],
            "group_name": rule["dip_group_name"],
            "billing_unit": rule["billing_unit"],
            "score": float(rule["dip_score"]),
            "ins_type": "自费",
            "benchmark_amount": None,
            "range_90_110": None
        }

    # Step 8: 从数据库字段动态读取参数计算基准金额（严禁硬编码系数）
    dip_score = Decimal(str(rule["dip_score"]))
    hosp_coef = Decimal(str(rule["hosp_coef"]))
    low_th = Decimal(str(rule["low_threshold"]))
    high_th = Decimal(str(rule["high_threshold"]))

    if is_employee:
        point_val = Decimal(str(rule["employee_point"]))
        base_unit = Decimal(str(rule["employee_base"]))
    else:
        point_val = Decimal(str(rule["resident_point"]))
        base_unit = Decimal(str(rule["resident_base"]))

    if is_bed_day:
        days_dec = Decimal(str(bed_days))
        benchmark = (base_unit * days_dec).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    else:
        benchmark = base_unit.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    # 零基准与无效基准核心层统一拦截（绝不返回 top-level MATCHED 或 benchmark_amount: 0.0）
    if benchmark <= Decimal("0"):
        return {
            "matched": False,
            "status": "PENDING",
            "pending_reason": "INVALID_BENCHMARK",
            "message": "命中规则基准金额为零或无效（<=0），需核实医保目录分值与点值参数，无法输出有效测算基准",
            "rule_id": rule["rule_id"],
            "rule_version": rule["rule_version"],
            "department": rule["department"],
            "disease_code": rule["icd_code"],
            "disease_name": rule["icd_name"],
            "group_name": rule["dip_group_name"],
            "ins_type": "职工" if is_employee else "居民"
        }

    range_lower = (benchmark * low_th).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    range_upper = (benchmark * high_th).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    return {
        "matched": True,
        "status": "MATCHED",
        "rule_id": rule["rule_id"],
        "rule_version": rule["rule_version"],
        "department": rule["department"],
        "disease_code": rule["icd_code"],
        "disease_name": rule["icd_name"],
        "group_name": rule["dip_group_name"],
        "operation_type": rule["op_name"] or "保守治疗",
        "billing_unit": rule["billing_unit"],
        "score": float(dip_score),
        "point_value": float(point_val),
        "hospital_coefficient": float(hosp_coef),
        "ins_type": "职工" if is_employee else "居民",
        "benchmark_amount": float(benchmark),
        "bed_days": bed_days if is_bed_day else None,
        "range_90_110": [float(range_lower), float(range_upper)],
        "low_threshold": float(low_th),
        "high_threshold": float(high_th)
    }

def calculate_benchmark(score: Decimal, ins_type: str, point_val: Optional[Decimal] = None, hosp_coef: Optional[Decimal] = None) -> Decimal:
    """兼容旧接口的基准金额测算函数"""
    norm = normalize_insurance_type(ins_type)
    is_emp = (norm.get("category") == "职工")
    pv = point_val if point_val is not None else (Decimal("7.5") if is_emp else Decimal("7.0"))
    hc = hosp_coef if hosp_coef is not None else Decimal("0.6")
    return (score * pv * hc).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

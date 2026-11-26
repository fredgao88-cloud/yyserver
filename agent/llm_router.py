# -*- coding: utf-8 -*-
"""
院内本地意图解析与规则分流模块（零云端调用，保护患者隐私）
严格遵循《开发设计文档 v1.2》与业务审核规范：
1. 渠道隔离：钉钉等外部移动端涉及患者级信息时启动隐私盾牌，不回显患者私密数据；
2. 金额口径澄清：主动识别“能报多少/报销”问题，澄清患者个人报销与医院 DIP 预估拨付额口径差异；
3. 参保类型严谨提取：缺失医保类型时触发 PENDING 追问，严禁默认“职工”；
4. 诊断精细区分：区分显式 ICD 编码与宽泛病名，宽泛病名缺少方案时触发候选追问，绝不盲目套用；
5. 操作时态感知：提取“拟行/已完成/已取消”时态，已取消操作自动降级为保守治疗或基础分组。
"""
import re
from decimal import Decimal
from typing import Dict, Any, Optional
from tools.dip_calculator import match_dip_rule, evaluate_traffic_light, normalize_insurance_type

def extract_operation_keyword(text_clean: str):
    """
    DIP-OP-001：按分句顺序建立操作状态机追踪每个具体操作词的最新状态：
    - 同一操作（含“胃镜检查”/“胃镜”等子串关联操作）后句状态覆盖前句；
    - 同一操作先拟行后完成（“拟行胃镜，随后已完成胃镜”）-> 取 COMPLETED；
    - 不同操作互不抹去（“已完成胃镜，拟行活检”）-> 保留已完成的胃镜；
    - 后句省略操作名直接取消（“胃镜检查，现已取消”）-> 将取消绑定到前序最近操作并回退保守；
    - 非手术事项计划（“已完成胃镜，计划出院”）-> 不影响手术状态。
    """
    candidate_ops = [
        ("切除术+胃镜", "胃镜检查"),
        ("切除+胃镜", "胃镜检查"),
        ("胃镜检查", "胃镜检查"),
        ("胃十二指肠镜", "胃十二指肠"),
        ("活检", "活检"),
        ("活组织", "活检"),
        ("直肠息肉切除", "直肠息肉切除"),
        ("胃息肉切除", "胃息肉切除"),
        ("电子结肠镜", "电子结肠镜"),
        ("结肠镜", "结肠镜"),
        ("保守", "保守"),
        ("胃镜", "胃镜"),
        ("切除", "切除"),
        ("治疗性操作", "综合-治疗性操作"),
        ("操作", "综合-治疗性操作"),
    ]
    cancel_kws = ("取消", "未做", "未行", "放弃", "暂不做")
    planned_kws = ("拟行", "拟做", "计划", "准备行", "拟实施", "准备做", "打算做", "原定")
    completed_kws = ("已完成", "已行", "已做", "做完", "术后", "已实施")
    non_op_nouns = ("出院", "入院", "手续", "转科", "复查", "随访", "回家", "床位", "病历", "签字", "缴费", "结算", "餐饮", "陪护")

    clauses = [c.strip() for c in re.split(r"[，,；;。！？!?]+", text_clean or "") if c.strip()]
    if not clauses:
        clauses = [text_clean or ""]

    op_states = {}
    last_op = None

    def _set_op_state(target_op: str, new_state: str):
        for existing_op in list(op_states.keys()):
            if existing_op == target_op or existing_op in target_op or target_op in existing_op:
                op_states[existing_op] = new_state
        op_states[target_op] = new_state

    _temporal_ref_re = re.compile(r"(?:该|此|上述)(?:操作|手术|检查)(?:之前|之后|前|后|时|中)")
    _pronoun_re = re.compile(r"(?:该操作|该手术|该检查|此操作|此手术|此检查|该项|上述操作|上述手术|上述检查|前项)")
    _tense_strip_re = re.compile(
        r"(?:现已|已经|已完成|已行|已做|做完|术后|已实施|暂不做|准备行|拟实施|准备做|打算做|"
        r"取消|未做|未行|放弃|拟行|拟做|计划|原定|原本|随后|后来|最终|确定|决定|还是|改为|变成|"
        r"完成|实施|执行|目前|已|暂|了|的|呢|\s)+"
    )

    for clause in clauses:
        # 先剥除时间状语参照（如“该检查前”），避免时间参照触发指代继承
        clause_no_temporal = _temporal_ref_re.sub("", clause)
        has_pronoun = bool(_pronoun_re.search(clause_no_temporal))

        matched_kw = None
        matched_pat = None
        for pattern, op_kw in candidate_ops:
            if pattern in clause_no_temporal:
                matched_kw = op_kw
                matched_pat = pattern
                break

        # 仅当无具体术式名（或仅被泛词“操作”匹配且句中带明确指代词如“该操作”）
        # 且剥除指代词与时态词后无其他独立事项（如“抽血”）时，才继承更新上一操作
        is_pure_pronoun_ref = has_pronoun and (matched_kw is None or matched_pat == "操作")
        if is_pure_pronoun_ref and last_op is not None:
            residual_after_pronoun = _tense_strip_re.sub("", _pronoun_re.sub("", clause_no_temporal)).strip()
            if not residual_after_pronoun:
                if any(k in clause for k in cancel_kws):
                    _set_op_state(last_op, "CANCELED")
                elif any(k in clause for k in completed_kws):
                    _set_op_state(last_op, "COMPLETED")
                elif any(k in clause for k in planned_kws):
                    if op_states.get(last_op) == "UNKNOWN":
                        _set_op_state(last_op, "PLANNED")
            continue

        if matched_kw is not None:
            last_op = matched_kw
            if any(k in clause for k in cancel_kws):
                _set_op_state(matched_kw, "CANCELED")
            elif any(k in clause for k in planned_kws):
                _set_op_state(matched_kw, "PLANNED")
            elif any(k in clause for k in completed_kws):
                _set_op_state(matched_kw, "COMPLETED")
            else:
                op_states.setdefault(matched_kw, "UNKNOWN")
        elif last_op is not None:
            residual = _tense_strip_re.sub("", clause).strip()
            if residual:
                continue
            if any(k in clause for k in cancel_kws):
                _set_op_state(last_op, "CANCELED")
            elif any(k in clause for k in completed_kws):
                _set_op_state(last_op, "COMPLETED")
            elif any(k in clause for k in planned_kws):
                if op_states.get(last_op) == "UNKNOWN":
                    _set_op_state(last_op, "PLANNED")

    if op_states:
        for op_kw, st in op_states.items():
            if st == "COMPLETED":
                return op_kw, "COMPLETED", ""
        for op_kw, st in op_states.items():
            if st == "UNKNOWN":
                return op_kw, "UNKNOWN", ""
        for op_kw, st in op_states.items():
            if st == "CANCELED":
                return "保守", "CANCELED", "（检测到手术/操作已取消，按保守治疗基础分组测算）"
        for op_kw, st in op_states.items():
            if st == "PLANNED":
                return "保守", "PLANNED", "（检测到操作为拟行/尚未执行状态，依规暂不计入手术操作组，按保守治疗分组测算；待实际执行后按新组结算）"

    if any(k in text_clean for k in cancel_kws):
        return "保守", "CANCELED", "（检测到手术/操作已取消，按保守治疗基础分组测算）"
    if any(k in text_clean for k in planned_kws):
        return "保守", "PLANNED", "（检测到操作为拟行/尚未执行状态，依规暂不计入手术操作组，按保守治疗分组测算；待实际执行后按新组结算）"
    return None, "UNKNOWN", ""


def parse_local_intent(text: str, channel: str = "web") -> Dict[str, Any]:
    """院内纯本地意图解析与参数提取（零云端调用）"""
    text_clean = text.strip()
    
    # ---------------- 1. 渠道隔离与钉钉隐私盾牌 ----------------
    if "dingtalk" in channel.lower():
        is_patient_query = bool(
            re.search(r'(\d+\s*床|床号|病床|大爷|大娘|大叔|阿姨|师傅|患者|病人|住院号|zy\d+|流水号|病案|姓名|花销|花了多少|费用|花了|花费|多少钱|自负|自付|自费|超标|预警|查房)', text_clean, re.I)
        )
        if is_patient_query:
            return {
                "intent": "privacy_shielded",
                "reply": "【院内业务安全提醒】根据医疗数据安全合规要求，涉及在院患者具体明细与控费预警信息不通过移动端回显。请使用工作站登录院内专属 HTTPS 工作台查看：\nhttps://hospital-internal.local/inpatient",
                "data": {"shielded": True, "channel": channel}
            }

    # ---------------- 2. 报销口径前置澄清拦截 ----------------
    # 当医生/用户咨询“能报多少/报销多少/个人自负”时，必须前置区分患者报销与医院DIP统筹，绝不直接将医院DIP预算当成患者报销金额
    has_reimburse_query = bool(re.search(r'(能报|报销|报多少|自负|自费多少|自理|自付多少)', text_clean))
    if has_reimburse_query:
        return {
            "intent": "need_reimbursement_clarification",
            "reply": (
                "【医保报销口径前置澄清】\n"
                "您询问了“能报多少/报销政策”，请注意区分以下两个完全不同的业务口径：\n"
                "1. 【患者端个人报销/自付】：需结合患者参保类型、起付线、乙类先行自付比例、分段报销比例及目录外自费，需由医保窗口以实时结算单为准。\n"
                "2. 【医院端 DIP 医保统筹支付基准】：医保局向医院按主要诊断分值、统筹区点值与医疗机构系数核算的定额预算总额。\n\n"
                "为避免误导患者或临床计费，请明确您需要咨询的是【医院端 DIP 预估基准】还是【患者个人自付政策】？"
            ),
            "data": {
                "matched": False,
                "status": "PENDING",
                "pending_reason": "NEED_REIMBURSEMENT_CLARIFICATION"
            }
        }
    reimburse_clarification = ""

    # ---------------- 3. 提取年龄 ----------------
    age = None
    age_match = re.search(r'(\d+)\s*岁', text_clean)
    if age_match:
        age = int(age_match.group(1))
        
    # ---------------- 4. 提取在院天数 / 床日 ----------------
    bed_days = None
    bed_match = re.search(r'(\d+)\s*(?:天|床日|日)', text_clean)
    if bed_match and ("床日" in text_clean or "住院" in text_clean or "在院" in text_clean or "康复" in text_clean):
        bed_days = int(bed_match.group(1))

    # ---------------- 5. 提取参保类型 (消歧、否定/既往过滤、指令优先与待查拦截) ----------------
    # 统一调用 normalize_insurance_type，确保与 match_dip_rule 共享同一套分句时态与修饰判定
    norm_ins = normalize_insurance_type(text_clean)
    
    # 5.1 检查是否显式包含活跃的“参保类型待查/未定”或“仅有否定险种”等表述（例如：“企业职工，参保类型 待查” / “本次未按职工医保结算”）
    # 凡输入明确指明参保待查或仅有否定险种，严禁按企业职工假定险种，一律阻断追问并省略金额
    if not norm_ins["valid"] and norm_ins.get("reason") == "MISSING_INS_TYPE" and any(k in (norm_ins.get("message") or "") for k in ("待查", "排除", "未说明实际参保类型")):
        return {
            "intent": "need_clarification",
            "reply": f"【需要明确参保类型】\n{norm_ins.get('message', '当前医保参保类型未明确')}。由于城镇职工与城乡居民的 DIP 点值与基准不同，在明确参保类型前系统依规暂不测算金额，请补充患者的具体险种（城镇职工 / 城乡居民 / 自费）。" + reimburse_clarification,
            "data": {
                "matched": False,
                "status": "PENDING",
                "pending_reason": "MISSING_INS_TYPE"
            }
        }

    # 5.2 多重模糊、职业状态或暂不适用医保统筹险种阻断拦截
    if not norm_ins["valid"]:
        if norm_ins.get("reason") == "AMBIGUOUS_INS_TYPE":
            return {
                "intent": "need_clarification",
                "reply": f"【需要明确参保类型】\n{norm_ins.get('message', '同时存在职工与居民医保且未指明结算口径，请明确患者本次住院按哪类险种结算')}。",
                "data": {
                    "matched": False,
                    "status": "PENDING",
                    "pending_reason": "AMBIGUOUS_INS_TYPE"
                }
            }
        elif norm_ins.get("reason") == "OCCUPATION_NOT_INSURANCE_TYPE":
            return {
                "intent": "need_clarification",
                "reply": "【需要明确参保类型】\n检测到您输入了职业状态（退休/在职等），但未指明具体医保参保类型。请明确患者是【城镇职工医保】、【城乡居民医保】还是【公费医疗/自费】（不同险种医保 DIP 测算基准与规则不同）。",
                "data": {
                    "matched": False,
                    "status": "PENDING",
                    "pending_reason": "OCCUPATION_NOT_INSURANCE_TYPE"
                }
            }
        elif norm_ins.get("reason", "").startswith("UNSUPPORTED_INS_TYPE"):
            return {
                "intent": "need_clarification",
                "reply": f"【险种暂不适用 DIP】\n{norm_ins.get('message')}",
                "data": {
                    "matched": False,
                    "status": "PENDING",
                    "pending_reason": norm_ins.get("reason")
                }
            }
        elif norm_ins.get("reason") == "CORRECTION_UNCONFIRMED":
            # “是否需要更正为X医保？”等更正询问：确认前绝不擅自更改测算口径，先澄清
            return {
                "intent": "need_clarification",
                "reply": f"【更正参保类型待确认】\n{norm_ins.get('message', '检测到参保类型更正询问，确认前系统暂不更改测算口径')}。",
                "data": {
                    "matched": False,
                    "status": "PENDING",
                    "pending_reason": "CORRECTION_UNCONFIRMED"
                }
            }

    ins_type = norm_ins.get("category") if norm_ins.get("valid") else None

    # ---------------- 6. 操作识别与时态感知 ----------------
    op_keyword, op_status, op_note = extract_operation_keyword(text_clean)

    # ---------------- 7. 识别病种 (区分显式 ICD 编码与宽泛病名) ----------------
    disease = None
    has_explicit_icd = False
    
    # 优先提取 ICD 编码 (如 M17.9, K63.5, I10.x, M53.1)
    icd_regex = re.search(r'\b([A-Z]\d{2}(?:\.[0-9a-zA-Z]{1,2})?)\b', text_clean, re.I)
    if icd_regex:
        disease = icd_regex.group(1).upper()
        has_explicit_icd = True
    else:
        # 常见疾病名称
        disease_names = [
            ("膝关节病", "膝关节病"),
            ("头痛", "头痛"),
            ("结肠息肉", "结肠息肉"),
            ("颈臂综合征", "颈臂综合征"),
            ("胃食管反流", "胃食管反流"),
            ("胃炎", "胃炎"),
            ("胃和十二指肠息肉", "胃和十二指肠息肉"),
            ("直肠息肉", "直肠息肉"),
            ("结肠良性肿瘤", "结肠良性肿瘤"),
            ("高血压", "高血压"),
            ("支气管炎", "支气管炎"),
            ("睡眠障碍", "睡眠障碍"),
            ("肩损害", "肩损害"),
            ("脑梗死", "脑梗死"),
            ("椎管狭窄", "椎管狭窄"),
            ("下背痛", "下背痛")
        ]
        for term, dname in disease_names:
            if term in text_clean:
                disease = dname
                has_explicit_icd = False
                break
            
    if not disease:
        return {
            "intent": "unknown",
            "reply": "您好，我是院内业务智能助手。您可以向我咨询病种 DIP 规则，例如：\n• '68岁职工，诊断编码 M17.9，保守治疗。请查询医院 DIP 预估支付额。'\n• '结肠息肉 K63.5 拟行切除术+胃镜检查，职工医保分值是多少？'\n• '颈臂综合征 M53.1 职工康复按床日住院12天'"
        }

    # ---------------- 8. 参保类型缺失阻断 ----------------
    if ins_type is None:
        return {
            "intent": "need_clarification",
            "reply": f"【需要补充参保类型】\n已识别病种：{disease}。\n由于城镇职工与城乡居民的 DIP 点值不同（职工 7.5，居民 7.0），测算基准存在明显差异。请补充患者的参保类型（城镇职工 / 城乡居民 / 自费）。" + reimburse_clarification,
            "data": {
                "matched": False,
                "status": "PENDING",
                "pending_reason": "MISSING_INS_TYPE",
                "disease": disease
            }
        }

    # ---------------- 9. 调用规则计算核心引擎 ----------------
    # 宽泛病名未指定方案、年龄区分、床日天数及参保类型合规等均由 match_dip_rule 核心引擎动态判定
    # 彻底杜绝在路由层硬编码规则分组、分值与金额，完全依赖数据库生效规则
    res = match_dip_rule(
        disease=disease,
        age=age,
        ins_type=ins_type,
        op_keyword=op_keyword,
        bed_days=bed_days
    )
    
    if res.get("status") == "MATCHED":
        reply = (
            f"【DIP 规则匹配成功】{op_note}\n"
            f"• 适用分组：{res['group_name']} (规则编号: #{res['rule_id']})\n"
            f"• 规则版本：{res['rule_version']} | 科室：{res['department']}\n"
            f"• DIP 分值：{res['score']} 分 | 计费方式：{res['billing_unit']}\n"
            f"• 参保类型：{res['ins_type']} (点值: {res['point_value']}, 医院系数: {res['hospital_coefficient']})\n"
            f"• 预估基准金额：￥{res['benchmark_amount']:.2f} 元\n"
            f"• 90%~110% 观察区间：￥{res['range_90_110'][0]:.2f} ~ ￥{res['range_90_110'][1]:.2f} 元"
        )
        if res.get("bed_days"):
            reply += f"\n• 测算床日天数：{res['bed_days']} 天"
            
        reply += reimburse_clarification
            
        return {
            "intent": "query_dip_rule",
            "reply": reply,
            "data": res
        }
    elif res.get("status") == "NOT_APPLICABLE":
        reply = (
            f"【不适用 DIP 医保支付】\n"
            f"• 患者参保类型为：自费\n"
            f"• 说明：{res['message']}。DIP 支付分值与基准仅适用于城镇职工与城乡居民基本医保统筹结算。"
            + reimburse_clarification
        )
        return {
            "intent": "query_dip_rule",
            "reply": reply,
            "data": res
        }
    elif res.get("status") == "PENDING":
        reply = f"【无法直接计算基准（待补充信息）】\n"
        reply += f"原因：{res['message']}\n"
        if res.get("candidates"):
            reply += "候选分组列表：\n"
            for c in res["candidates"]:
                grp_name = c.get('dip_group_name') or ''
                op_name = c.get('op_name') or ''
                aux = c.get('aux_detail') or ''
                details = " | ".join([x for x in [op_name, aux] if x])
                detail_str = f"（{details}）" if details else ""
                reply += f"• #{c['rule_id']} {grp_name}{detail_str}\n"
            reply += "在未明确具体治疗方案前，系统按合规要求暂不测算并暂不输出金额，请补充具体的临床诊断编码或明确治疗方式（保守或手术操作）。"
        reply += reimburse_clarification
        return {
            "intent": "need_clarification",
            "reply": reply,
            "data": res
        }
    else:
        return {
            "intent": "query_dip_rule",
            "reply": res.get("message", "未检索到匹配的 DIP 规则。") + reimburse_clarification,
            "data": res
        }

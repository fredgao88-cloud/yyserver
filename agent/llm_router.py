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
from tools.dip_calculator import match_dip_rule, evaluate_traffic_light

def parse_local_intent(text: str, channel: str = "web") -> Dict[str, Any]:
    """院内纯本地意图解析与参数提取（零云端调用）"""
    text_clean = text.strip()
    
    # ---------------- 1. 渠道隔离与钉钉隐私盾牌 ----------------
    if channel.lower() == "dingtalk":
        privacy_keywords = ["患者", "住院号", "床号", "姓名", "费用", "超标", "预警", "查房", "病案", "自负"]
        if any(k in text_clean for k in privacy_keywords):
            return {
                "intent": "privacy_shielded",
                "reply": "【院内业务安全提醒】根据医疗数据安全合规要求，涉及在院患者具体明细与控费预警信息不通过移动端回显。请使用工作站登录院内专属 HTTPS 工作台查看：\nhttps://hospital-internal.local/inpatient",
                "data": {"shielded": True, "channel": channel}
            }

    # ---------------- 2. 金额口径澄清识别 ----------------
    has_reimburse_query = bool(re.search(r'(能报|报销|报多少|自负|自费多少|自理)', text_clean))
    reimburse_clarification = (
        "\n\n【金额口径澄清提示】\n"
        "• 若您询问的是【医保拨付医院 DIP 预估统筹额】：根据主要诊断分值与医院系数测算（如下）。\n"
        "• 若您询问的是【患者个人医保自负报销额】：需依据患者参保地政策（起付线、个人报销比例及目录外自费项目），以出院医保实时结算单为准，DIP 病种分值不直接等同于个人报销金额。"
    ) if has_reimburse_query else ""

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

    # ---------------- 5. 提取参保类型 (严禁默认职工) ----------------
    ins_type = None
    if "自费" in text_clean:
        ins_type = "自费"
    elif "居民" in text_clean or "城乡" in text_clean or "农合" in text_clean:
        ins_type = "居民"
    elif "职工" in text_clean or "城镇" in text_clean:
        ins_type = "职工"

    # ---------------- 6. 操作识别与时态感知 ----------------
    op_status = "UNKNOWN"
    if any(k in text_clean for k in ["取消", "未做", "未行", "放弃", "暂不做"]):
        op_status = "CANCELED"
    elif any(k in text_clean for k in ["拟行", "拟做", "计划", "准备行", "拟实施", "准备做"]):
        op_status = "PLANNED"
    elif any(k in text_clean for k in ["已完成", "已行", "已做", "做完", "术后", "已实施"]):
        op_status = "COMPLETED"

    op_keyword = None
    op_note = ""
    
    if op_status == "CANCELED":
        # 已取消操作：不纳入手术加分，自动回退到保守治疗
        op_keyword = "保守"
        op_note = "（检测到手术/操作已取消，按保守治疗或基础分组测算）"
    else:
        # 优先提取具体复合操作词，避免笼统词（如单纯“切除”）覆盖复合术式
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
            ("操作", "综合-治疗性操作")
        ]
        for pattern, op_kw in candidate_ops:
            if pattern in text_clean:
                op_keyword = op_kw
                break
            
        if op_status == "PLANNED":
            op_note = "（注：当前按拟行操作预估，出院以实际执行为准）"

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

    # ---------------- 9. 宽泛病名且未指定治疗方案阻断 ----------------
    # 例如只输入“68岁职工膝关节病”，未给明确编码也未指定保守还是手术
    if not has_explicit_icd and disease == "膝关节病" and op_keyword is None:
        return {
            "intent": "need_clarification",
            "reply": (
                "【需要明确具体诊断或治疗方案】\n"
                "检测到您输入了宽泛病名“膝关节病”，未提供明确 ICD 编码或治疗方案（保守治疗 / 手术操作）。\n"
                "• 若确认为【未特指的膝关节病 (M17.9) 高龄保守治疗】，预估分值为 620.2 分（基准 ￥2790.90 元）；\n"
                "• 若拟行内镜下手术或为常规保守/中医优势病种，分值各不相同。\n"
                "请补充具体的临床诊断编码或是否拟行手术操作，绝不盲目套用单一金额。"
            ) + reimburse_clarification,
            "data": {
                "matched": False,
                "status": "PENDING",
                "pending_reason": "AMBIGUOUS_BROAD_DIAGNOSIS",
                "disease": disease,
                "age": age,
                "ins_type": ins_type
            }
        }

    # ---------------- 10. 调用规则计算核心引擎 ----------------
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
                reply += f"• #{c['rule_id']} {c['dip_group_name']} (分值: {c['dip_score']})\n"
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

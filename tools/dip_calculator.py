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

# ----------------------------------------------------
# 统一医保别名定义 (肯定匹配与否定匹配共用同一套底表)
# ----------------------------------------------------
INS_ALIAS_EMP = r'(?:城镇职工医保|城镇职工|职工医保|单位职工|企业职工|在职职工|职工)'
INS_ALIAS_RES = r'(?:城乡居民医保|城镇居民医保|城乡居民|城镇居民|居民医保|新型农村合作医疗|新农合|农村合作医疗|农合|居民)'
INS_ALIAS_GF  = r'(?:公费医疗|公费)'
INS_ALIAS_GS  = r'(?:工伤保险|工伤)'
INS_ALIAS_ZF  = r'(?:全额自费|自费患者|纯自费|全自费|自费)'

# 综合正则（按长度降序排列，避免短词贪婪截断长词，如避免“居民”提前截断“城乡居民”）
ALL_INS_ALIASES = [
    r'新型农村合作医疗', r'城镇职工医保', r'城乡居民医保', r'城镇居民医保',
    r'农村合作医疗', r'城镇职工', r'单位职工', r'企业职工', r'在职职工', r'职工医保',
    r'城乡居民', r'城镇居民', r'居民医保', r'公费医疗', r'工伤保险',
    r'全额自费', r'自费患者', r'纯自费', r'全自费', r'新农合',
    r'职工', r'居民', r'公费', r'工伤', r'农合', r'自费'
]
INS_ALIAS_COMBINED = r'(?:' + '|'.join(ALL_INS_ALIASES) + r')'

NEGATION_PREFIXES = r'(?:非|不按|未按|不采用|未采用|排除|除外|不是|不属于|否认|不使用|未使用|不以|未以)'

# 项目级范围限定词（前置出现时说明自费仅限具体项目，非整单险种）
ITEM_SCOPE_PREFIXES = r'(?:耗材|材料|药品|西药|中药|草药|药|检查|检验|化验|治疗|手术耗材|特需|服务|部分项目|项目|部分|目录外|丙类)'

# 整单范围词：出现在项目范围词与“自费”之间时，说明是整单自费指令而非项目级自费
WHOLE_BILL_SCOPE = r'(?:本次住院|整次住院|住院费用|整单|整次|本次费用)'

def has_whole_bill_self_pay(c: str) -> bool:
    """整单自费判定：项目范围词（如“检查”）后出现“本次住院/整单”等整单范围词再接自费结算，
    则该指令作用于整次住院（如“经检查本次住院按自费结算”），而非项目级自费。
    整单范围词的优先级高于词语距离；
    但整单范围词之后若再次出现项目限定（如“经检查本次住院仅耗材按自费结算”中的“仅耗材”），
    自费范围被重新收窄到具体项目，不属于整单自费。"""
    for m in re.finditer(
        r'(?:' + ITEM_SCOPE_PREFIXES + r')[^，。；,;]{0,8}?(?:' + WHOLE_BILL_SCOPE + r')([^，。；,;]{0,6}?)(?:按|以|属于|采用)\s*自费',
        c
    ):
        gap_after_scope = m.group(1)
        if re.search(ITEM_SCOPE_PREFIXES, gap_after_scope) or re.search(r'(?:仅|只是|只有|仅限)', gap_after_scope):
            continue  # 整单范围词后又限定具体项目，自费仅限该项目
        return True
    return False

def normalize_insurance_type(ins_type_str: Optional[str]) -> Dict[str, Any]:
    """
    统一参保类型归一化与前置合规审查核心函数 (Round 14 优化版)：
    1. 统一别名库：否定匹配与肯定匹配 100% 共享相同别名表，精准支持“城乡居民”等复合险种；
    2. 针对性选择疑问句识别：仅当选择词连接的是两类及以上不同险种时触发 AMBIGUOUS_INS_TYPE，
       若用户已确认险种而后文仅询问临床方案（如“保守还是手术”），绝不误判险种歧义；
       且选择词判定只在非历史、非否定的活跃分句内进行，历史疑问不压制当前确认；
    3. “更正为/修改为/调整为”明确覆盖：后置更正指令强力覆盖既往/前置指令；
       但“暂不更正/不更正/无需更正”等明确否定保留原险种；“是否需要更正为…”“需要更正为…吗”
       等询问形式（含句尾“吗/呢/吧”疑问标记与分句句尾问号），以及“待核实后再更正为…”
       “如果符合条件则更正为…”等条件性更正（前提未满足），一律先澄清（CORRECTION_UNCONFIRMED），
       绝不擅自更改测算口径；
       多次更正并存时按(分句序号, 分句内字符位置)判断最后一次更正请求的状态：
       询问/条件更正在后则澄清，确认更正在后则以确认更正为准；
    4. 支付范围前置隔离：“耗材按自费项目结算”“进口耗材单独按自费项目结算”精准判定为
       项目级自费（范围词与动词间允许“单独/需要”等填充词），整单按基本医保正常测算；
       但“经检查本次住院按自费结算”等含整单范围词（本次住院/整单）的指令优先于词语距离，按整单自费处理；
    5. 待查多符号与多字段解耦：冒号、破折号无缝支持，参保待查与病理待查互不干扰；
    6. 仅有否定险种强力拦截并省略金额。
    """
    if not ins_type_str or not str(ins_type_str).strip():
        return {
            "valid": False,
            "category": None,
            "status": "PENDING",
            "reason": "MISSING_INS_TYPE",
            "pending_reason": "MISSING_INS_TYPE",
            "message": "缺少有效参保类型（职工/居民/自费），无法确定对应点值与测算基准"
        }

    s = str(ins_type_str).strip()

    # 1. 分句拆分：按中英文逗号、句号、分号、问号、感叹号、换行符分割
    # 保留句尾问号标记：问号是“更正为居民医保？”等更正询问的唯一疑问信号，分句时不得丢弃，
    # 否则询问会被当作已确认的更正指令直接执行并返回错误金额
    raw_parts = re.split(r'([,，;；。?!？！\n]+)', s)
    clauses = []  # 每项为 (分句文本, 是否以问号结尾)
    for pi in range(0, len(raw_parts), 2):
        text_part = (raw_parts[pi] or '').strip()
        if not text_part:
            continue
        delim = raw_parts[pi + 1] if pi + 1 < len(raw_parts) else ''
        ends_question = bool(delim) and ('?' in delim or '？' in delim)
        clauses.append((text_part, ends_question))
    clause_info = []

    for c, c_ends_question in clauses:
        is_historical = bool(re.search(r'(?:原|既往|过去|曾|之前|既往的)', c))
        is_negated = bool(re.search(NEGATION_PREFIXES, c))
        is_closed = bool(re.search(r'(?:已结案|已结清|结案|已终结)', c))

        # 检查是否包含更正指令（如“更正为居民医保”、“核实后更正为...”）
        is_correction = bool(re.search(r'(?:更正为|修改为|调整为|变更为|改为|重新按|重新核准为|核实后为)', c))
        is_current_directive = bool(re.search(r'(?:本次|现|当前|此次|现已|确认|转为|以|按)', c)) or is_correction

        # 检查参保类型待查：
        # 支持冒号（中文全角 ： 与英文半角 :）、空格、破折号、等号、“为”
        # 且与病理/手术/诊断等临床类型完全解耦，独立匹配
        has_ins_pending = False
        if re.search(r'(?:参保类型|参保险种|险种类型|医保类型|参保身份|参保|险种|医保)[：:\s\-为=是]*(?:待查|待定|未定|未知|不详|不清|未明确|暂不知|暂未确定|未确定)', c):
            has_ins_pending = True
        elif re.search(r'^(?:待查|待定|未定|未知|未明确|不详)$', c) and any(k in s for k in ('参保', '险种', '医保类型')):
            has_ins_pending = True

        # 检查是否属于项目级自费
        # 包含：明确词“部分项目自费/自费项目”或者“<项目范围>按自费/自费结算”
        # 范围词与动词之间允许少量填充词（如“单独/需要/均”），如“进口耗材单独按自费项目结算”
        # 但若范围词与自费动词之间出现“本次住院/整单”等整单范围词，则属于整单自费（如“经检查本次住院按自费结算”）
        is_item_level_self_pay = False
        if re.search(r'(?:部分项目自费|项目自费|自费项目|部分自费|自费药品|自费耗材|自费药|自费部分|目录外自费)', c):
            is_item_level_self_pay = True
        elif not has_whole_bill_self_pay(c) and re.search(ITEM_SCOPE_PREFIXES + r'[^\w\s]*(?:按|以|属于|采用)?\s*自费', c):
            is_item_level_self_pay = True
        elif not has_whole_bill_self_pay(c) and re.search(ITEM_SCOPE_PREFIXES + r'[^，。；,;、\s]{1,6}(?:按|以|属于|采用)\s*自费', c):
            is_item_level_self_pay = True

        clause_info.append({
            'text': c,
            'is_historical': is_historical,
            'is_negated': is_negated,
            'is_closed': is_closed,
            'is_correction': is_correction,
            'is_current': is_current_directive,
            'has_ins_pending': has_ins_pending,
            'is_item_self_pay': is_item_level_self_pay,
            'is_question': c_ends_question
        })

    # 2. 检查是否存在活跃的待查表述（历史时态的'原参保类型待查'若伴随当前确认指令，不压制当前确认）
    has_active_current_confirm = any(
        ci['is_current'] and not ci['is_negated'] and not ci['is_historical'] and not ci['has_ins_pending']
        and any(k in ci['text'] for k in ('职工', '居民', '公费', '工伤', '自费'))
        for ci in clause_info
    )
    active_pending_clauses = [
        ci for ci in clause_info
        if ci['has_ins_pending'] and not (ci['is_historical'] and has_active_current_confirm)
    ]
    if active_pending_clauses:
        return {
            'valid': False,
            'category': None,
            'status': 'PENDING',
            'reason': 'MISSING_INS_TYPE',
            'pending_reason': 'MISSING_INS_TYPE',
            'message': f"检测到参保类型待查（{active_pending_clauses[0]['text']}），需明确参保类型后测算"
        }

    # 3. 提取结算指令与更正指令
    # 注意：若指令前存在“耗材/药品/检查”等项目修饰词，属于项目自费，严禁提取为整单结算指令！
    directive_records = []  # 记录 (category, is_correction, clause_index, pos)
    correction_question_target = None  # “是否需要更正为X”等询问形式的目标险种（先澄清，绝不擅自更改）
    correction_question_clause_idx = -1  # 更正询问所在分句序号
    correction_question_pos = -1  # 更正询问在分句内的字符位置（与分句序号共同定位最后一次更正请求）

    # 指令匹配正则：支持“本次按/采用/转为/以”以及更正词“更正为/修改为/调整为”
    # 使用统一别名表 INS_ALIAS_COMBINED
    dir_verb = r'(?:更正为|修改为|调整为|变更为|改为|重新按|重新核准为|核实后为|按|以|采用|转为)'
    dir_prefix = r'(?:本次|现|当前|此次|现已|确认|核实后)?\s*'
    combined_dir_regex = re.compile(dir_prefix + f'({dir_verb})' + r'\s*' + f'({INS_ALIAS_COMBINED})' + r'(?:医保)?(?:结算)?')

    # 更正动词前置否定/疑问识别（如“暂不更正为居民医保”/“是否需要更正为居民医保？”）
    corr_neg_pre = re.compile(r'(?:暂不|不需要|无需|无须|不予|勿|别|不|未)$')
    corr_question_pre = re.compile(r'(?:是否需要|要不要|需要|能否|可否|可不可以|是否)$')
    # 条件性/待核实更正识别（如“待核实后再更正为居民医保”/“如果符合条件则更正为居民医保”）：
    # 前提条件尚未满足，更正未生效，确认前绝不执行
    corr_conditional_pre = re.compile(r'(?:待核实|待确认|待核准|待审批|如果|若|假如|一旦|必要时|视情况|符合条件)')
    # 前提已落实识别（如“已核实符合条件并确认更正为居民医保”）：
    # 条件词与“已核实/已确认/已审批”等完成态确认标记共存时，说明前提已有明确核实结果，
    # 不再视为未决前提，更正正常生效
    corr_fulfilled_pre = re.compile(r'(?:已核实|经核实|核实无误|已确认|确认无误|已审批|已核准|已获批|已满足|已符合)')
    # 假设语气识别（“如果/若/假如/一旦”）：假设句内的完成态词（如“如果已获批则…”）只是假设前提，
    # 绝非事实确认，完成态词不得放行使更正生效（DIP-INS-001 变体）；
    # 仅当后续分句以陈述事实口吻明确确认同一前提结果（含完成态词且无假设词）时才解除待定（DIP-INS-002 变体）
    corr_hypothetical = re.compile(r'(?:如果|若|假如|一旦)')

    def _premise_core_confirmed(core_text, clause_text):
        """跨分句前提解除判据（DIP-INS-001 变体）：确认分句须覆盖前提核心内容的二字片段，
        避免“已核实年龄”等无关确认误解除“如果审批通过”的待定"""
        if not core_text:
            return False  # 前提核心为空时绝不自动解除，保持澄清
        if core_text in clause_text:
            return True
        return any(core_text[i:i + 2] in clause_text for i in range(len(core_text) - 1))

    pending_cond_clause_idx = -1  # 最近一个含未决前提（有条件词且无完成态确认）的分句序号；该前提修饰其后第一条更正指令
    pending_cond_core = ""  # 该未决前提的核心内容（剥除假设/条件标记与标点），用于核对后续确认是否针对同一前提
    for idx, ci in enumerate(clause_info):
        if ci['is_historical'] or ci['is_negated']:
            continue
        c = ci['text']

        # 本分句是否含未决前提：出现“如果/待核实/符合条件”等条件词，
        # 且无“已核实/已确认”等前提已落实标记（“如果审批通过”为未决，“已核实符合条件”为已落实）；
        # 假设句（如果/若/假如/一旦）内的完成态词（如“如果已获批”）只是假设前提，绝不视为已落实
        clause_has_pending_cond = bool(corr_conditional_pre.search(c)) and (
            bool(corr_hypothetical.search(c)) or not bool(corr_fulfilled_pre.search(c))
        )

        for m in combined_dir_regex.finditer(c):
            verb = m.group(1)
            raw_tgt = m.group(2)
            start_pos = m.start()
            pre_text = c[:start_pos].strip()

            is_corr_verb = any(k in verb for k in ('更正', '修改', '调整', '变更', '改'))

            # 否定形式的更正指令（“暂不更正为居民医保”）：不提取该指令，原险种指令保持不变
            if is_corr_verb and corr_neg_pre.search(pre_text):
                continue

            # 跨分句未决前提（如“如果审批通过，再更正为居民医保”中前一分句的“如果审批通过”）：
            # 将其与其修饰的下一条更正关联，仅作用于其后第一条结算/更正指令，命中即消费，
            # 绝不污染更靠后的已确认指令（如“…，已确认更正为职工”仍以确认更正为准）
            cross_clause_conditional = False
            if 0 <= pending_cond_clause_idx < idx:
                if (bool(corr_fulfilled_pre.search(c)) and not bool(corr_hypothetical.search(c))
                        and _premise_core_confirmed(pending_cond_core, c)):
                    # 后续分句以陈述事实口吻明确确认了**同一前提**的结果
                    # （如“现已确认审批通过并更正为居民医保”对应“如果审批通过”），
                    # 解除跨分句待定，更正正常生效（DIP-INS-002 变体）；
                    # 确认内容与前提无关的（如“已核实年龄”对应“如果审批通过”）绝不解除（DIP-INS-001 变体）
                    pending_cond_clause_idx = -1
                    pending_cond_core = ""
                else:
                    cross_clause_conditional = is_corr_verb
                    pending_cond_clause_idx = -1
                    pending_cond_core = ""

            # 疑问/条件性更正（“是否需要更正为居民医保？”/“需要更正为居民医保吗？”/“更正为居民医保？”/
            # “待核实后再更正为居民医保”/“如果符合条件则更正为居民医保”）：先澄清，绝不擅自更改口径
            # 除动词前的疑问词外，句尾“吗/呢/吧”疑问语气词、分句句尾问号，以及“待核实/如果/若”等
            # 条件前提标记，同样表明更正尚未生效，确认前严禁执行；
            # 但条件词处同时存在“已核实/已确认”等前提已落实标记的（如“已核实符合条件并确认更正为X”），
            # 说明前提已有明确核实结果，更正生效，绝不误拦截；
            # 假设句（如果/若/假如/一旦）内的完成态词（如“如果已获批则更正为X”）只是假设前提，绝不放行
            post_text = c[m.end():].strip()
            pre_conditional = bool(corr_conditional_pre.search(pre_text)) and (
                bool(corr_hypothetical.search(pre_text)) or not bool(corr_fulfilled_pre.search(pre_text))
            )
            is_question_form = (
                bool(corr_question_pre.search(pre_text))
                or bool(re.search(r'(?:吗|嘛|么|呢|吧)$', post_text))
                or ci['is_question']
                or pre_conditional
                or cross_clause_conditional
            )
            if is_corr_verb and is_question_form:
                q_t = raw_tgt
                if re.search(INS_ALIAS_GF, q_t):
                    correction_question_target = '公费医疗'
                elif re.search(INS_ALIAS_GS, q_t):
                    correction_question_target = '工伤保险'
                elif re.search(INS_ALIAS_ZF, q_t):
                    correction_question_target = '自费'
                elif re.search(INS_ALIAS_RES, q_t):
                    correction_question_target = '居民'
                elif re.search(INS_ALIAS_EMP, q_t):
                    correction_question_target = '职工'
                correction_question_clause_idx = idx
                correction_question_pos = start_pos
                continue

            # 检查指令前面是否紧挨着“耗材/药品/检查”等项目范围限定词
            # 如“耗材按自费项目结算”中的“按自费”；
            # 范围词与动词之间允许少量填充词（如“进口耗材单独按自费项目结算”），仅对自费目标生效
            is_self_pay_target = bool(re.search(INS_ALIAS_ZF, raw_tgt))
            # 项目范围词守卫不适用于整单自费指令：“本次住院/整单”等整单范围词优先于词语距离
            # （如“经检查本次住院按自费结算”中虽有“检查”，但明确作用于整次住院，绝不跳过）
            if is_self_pay_target and not has_whole_bill_self_pay(c) and re.search(ITEM_SCOPE_PREFIXES + r'[^，。；,;、\s]{0,6}$', pre_text):
                # 属于项目范围自费，绝不作为整单住院指令！
                continue
            if not is_self_pay_target and re.search(ITEM_SCOPE_PREFIXES + r'[^\w\s]*$', pre_text):
                continue

            matched_cat = None
            if re.search(INS_ALIAS_GF, raw_tgt):
                matched_cat = '公费医疗'
            elif re.search(INS_ALIAS_GS, raw_tgt):
                matched_cat = '工伤保险'
            elif re.search(INS_ALIAS_ZF, raw_tgt):
                matched_cat = '自费'
            elif re.search(INS_ALIAS_RES, raw_tgt):
                matched_cat = '居民'
            elif re.search(INS_ALIAS_EMP, raw_tgt):
                matched_cat = '职工'

            if matched_cat:
                is_corr = ci['is_correction'] or any(k in verb for k in ('更正', '修改', '调整', '变更', '改'))
                directive_records.append({
                    'category': matched_cat,
                    'is_correction': is_corr,
                    'clause_idx': idx,
                    'pos': start_pos,
                    'text': m.group(0)
                })

        # 分句处理完毕：若本分句含未决前提且前提目标不在本分句内（本分句无更正动词，
        # 如同分句的“如果符合条件则更正为X”已由动词前条件词判定），则登记为跨分句未决前提，
        # 修饰其后第一条更正指令（如“如果审批通过，再更正为居民医保”）
        if clause_has_pending_cond and not ci['is_correction'] and pending_cond_clause_idx < 0:
            pending_cond_clause_idx = idx
            # 记录前提核心内容（剥除假设/条件标记、语气词与标点），供后续分句确认同一前提时比对
            _core = re.sub(r'(?:如果|若|假如|一旦|待核实|待确认|待核准|待审批|必要时|视情况|则|就|再|的|了|吗|呢)', '', c)
            pending_cond_core = re.sub(r'[\s，。；,;、？?！!：:（）()【】"“”]+', '', _core)

    # 3.0 更正询问未确认：先澄清，保留当前口径，绝不擅自更改险种
    # 按语句先后判断最后一次更正请求的状态：以(分句序号, 分句内字符位置)精确定位，
    # 仅当更正询问出现在最后一次已确认更正之后时才阻断（同一分句内的多次更正也能区分先后）；
    # 若询问之后已有明确确认更正（如“是否需要更正为居民？已确认更正为职工”），以确认的更正为准
    last_confirmed_corr_key = max(
        ((d['clause_idx'], d.get('pos', -1)) for d in directive_records if d['is_correction']),
        default=(-1, -1)
    )
    if correction_question_target and (correction_question_clause_idx, correction_question_pos) > last_confirmed_corr_key:
        return {
            'valid': False,
            'category': None,
            'status': 'PENDING',
            'reason': 'CORRECTION_UNCONFIRMED',
            'pending_reason': 'CORRECTION_UNCONFIRMED',
            'message': f"检测到参保类型更正请求（目标：{correction_question_target}），更正尚未确认或前提条件未满足。请明确回复「确认更正为{correction_question_target}」或「暂不更正」；确认前系统按合规要求暂不更改测算口径"
        }

    # 3.1 检查是否存在明确的险种间选择疑问句（连接了两个及以上不同险种候选）
    # 例如：“按职工医保还是按居民医保结算？”、“按职工医保或居民医保结算？”
    # 必须限定在险种与险种之间的选择，绝不误拦截“保守还是手术”！
    # 且仅在非历史、非否定的活跃分句内判定：历史疑问（如“原先不确定按职工还是居民”）
    # 不得压制后续“现已核实按居民医保结算”的当前确认指令。
    ins_choice_pattern = re.compile(
        rf'(?:按|以|采用)?\s*(?:{INS_ALIAS_COMBINED})\s*(?:还是|或者|或|亦或)\s*(?:按|以|采用)?\s*(?:{INS_ALIAS_COMBINED})'
    )
    active_text_for_choice = '，'.join(
        ci['text'] for ci in clause_info if not ci['is_historical'] and not ci['is_negated']
    )
    has_ins_choice = bool(ins_choice_pattern.search(active_text_for_choice))

    # 如果指令列表中存在多个不同的非更正指令，或者文本中存在险种间的选择连词：
    distinct_dir_cats = list(dict.fromkeys([d['category'] for d in directive_records if not d['is_correction']]))
    if has_ins_choice or len(distinct_dir_cats) > 1:
        return {
            'valid': False,
            'category': None,
            'status': 'PENDING',
            'reason': 'AMBIGUOUS_INS_TYPE',
            'pending_reason': 'AMBIGUOUS_INS_TYPE',
            'message': '输入包含选择性或冲突的参保结算指令（如“按职工还是按居民”），请明确患者本次住院具体按哪类险种结算（城镇职工 / 城乡居民）'
        }

    # 3.2 检查更正指令优先覆盖（如“本次按职工医保结算，核实后更正为居民医保”）
    # 后置更正指令强力覆盖先前的任何指令
    correction_records = [d for d in directive_records if d['is_correction']]
    if correction_records:
        final_correction = correction_records[-1]['category']
        if final_correction == '公费医疗':
            return {'valid': False, 'category': None, 'status': 'PENDING', 'reason': 'UNSUPPORTED_INS_TYPE:公费医疗', 'pending_reason': 'UNSUPPORTED_INS_TYPE:公费医疗', 'message': '公费医疗统筹区尚未核准 DIP 映射，不适用基本医保 DIP 测算基准，需人工核实'}
        elif final_correction == '工伤保险':
            return {'valid': False, 'category': None, 'status': 'PENDING', 'reason': 'UNSUPPORTED_INS_TYPE:工伤保险', 'pending_reason': 'UNSUPPORTED_INS_TYPE:工伤保险', 'message': '工伤保险由工伤保险基金单独支付，不适用基本医保 DIP 分值付费测算'}
        elif final_correction == '自费':
            return {'valid': True, 'category': '自费', 'status': 'NOT_APPLICABLE', 'reason': 'SELF_PAY', 'pending_reason': 'SELF_PAY', 'message': '自费患者不适用医保 DIP 支付规则'}
        elif final_correction in ('居民', '职工'):
            return {'valid': True, 'category': final_correction, 'status': 'MATCHED', 'reason': None, 'pending_reason': None, 'message': None}

    # 3.3 检查唯一确定的肯定结算指令（无更正、无歧义冲突）
    if len(directive_records) == 1:
        single_dir = directive_records[0]['category']
        if single_dir == '公费医疗':
            return {'valid': False, 'category': None, 'status': 'PENDING', 'reason': 'UNSUPPORTED_INS_TYPE:公费医疗', 'pending_reason': 'UNSUPPORTED_INS_TYPE:公费医疗', 'message': '公费医疗统筹区尚未核准 DIP 映射，不适用基本医保 DIP 测算基准，需人工核实'}
        elif single_dir == '工伤保险':
            return {'valid': False, 'category': None, 'status': 'PENDING', 'reason': 'UNSUPPORTED_INS_TYPE:工伤保险', 'pending_reason': 'UNSUPPORTED_INS_TYPE:工伤保险', 'message': '工伤保险由工伤保险基金单独支付，不适用基本医保 DIP 分值付费测算'}
        elif single_dir == '自费':
            return {'valid': True, 'category': '自费', 'status': 'NOT_APPLICABLE', 'reason': 'SELF_PAY', 'pending_reason': 'SELF_PAY', 'message': '自费患者不适用医保 DIP 支付规则'}
        elif single_dir in ('居民', '职工'):
            return {'valid': True, 'category': single_dir, 'status': 'MATCHED', 'reason': None, 'pending_reason': None, 'message': None}

    # 4. 收集非历史、排除否定与结案短语后的活跃候选险种
    active_tokens = []
    negated_tokens = []
    has_ambiguous_urban = False
    has_occupation_only = False
    has_pure_self_pay = False

    negation_matcher = re.compile(NEGATION_PREFIXES + r'\s*(' + INS_ALIAS_COMBINED + r')')
    negation_cleaner = re.compile(NEGATION_PREFIXES + r'\s*(?:' + INS_ALIAS_COMBINED + r')')

    for ci in clause_info:
        if ci['is_historical']:
            continue
        c = ci['text']

        # 4.1 提取被明确否定的险种（共用同一套 INS_ALIAS_COMBINED 底表）
        for neg_m in negation_matcher.finditer(c):
            raw_neg = neg_m.group(1)
            if re.search(INS_ALIAS_EMP, raw_neg):
                negated_tokens.append('职工')
            elif re.search(INS_ALIAS_RES, raw_neg):
                negated_tokens.append('居民')
            elif re.search(INS_ALIAS_GF, raw_neg):
                negated_tokens.append('公费医疗')
            elif re.search(INS_ALIAS_GS, raw_neg):
                negated_tokens.append('工伤保险')
            elif re.search(INS_ALIAS_ZF, raw_neg):
                negated_tokens.append('自费')

        # 清除所有被否定的险种短语与已结案工伤
        c_clean = negation_cleaner.sub('', c)
        c_clean = re.sub(r'工伤(?:已结案|已结清|结案)', '', c_clean)

        # 清除被否定的更正指令整段（如“暂不更正为居民医保”/“无需更正为职工医保”）：
        # 该类分句不更改测算口径，其目标险种绝不计入活跃候选，否则“职工医保+暂不更正为居民医保”会被误判为险种冲突
        c_clean = re.sub(r'(?:暂不|不需要|无需|无须|不予|勿|别|不|未)\s*(?:更正为|修改为|调整为|变更为|改为|重新按|重新核准为|核实后为)\s*(?:' + INS_ALIAS_COMBINED + r')\s*(?:医保)?\s*(?:结算)?', '', c_clean)

        # 检查项目级自费：若属于项目级自费，彻底消除该自费短语，不作为整单险种
        # 范围词与动词之间允许少量填充词（如“进口耗材单独按自费项目结算”）
        if ci['is_item_self_pay']:
            c_clean = re.sub(r'(?:部分项目自费|项目自费|自费项目|部分自费|自费药品|自费耗材|自费药|自费部分|目录外自费)', '', c_clean)
            c_clean = re.sub(ITEM_SCOPE_PREFIXES + r'[^\w\s]*(?:按|以|属于|采用)?\s*自费[^\w\s]*(?:项目|结算)?', '', c_clean)
            c_clean = re.sub(ITEM_SCOPE_PREFIXES + r'[^，。；,;、\s]{1,6}(?:按|以|属于|采用)\s*自费[^\w\s]*(?:项目|结算)?', '', c_clean)

        # 提取活跃险种词
        if any(k in c_clean for k in ('全额自费', '纯自费', '全自费', '自费患者', '无医保')):
            active_tokens.append('自费')
            has_pure_self_pay = True
        elif '自费' in c_clean and not ci['is_item_self_pay']:
            active_tokens.append('自费')

        if re.search(INS_ALIAS_GF, c_clean):
            active_tokens.append('公费医疗')
        if re.search(INS_ALIAS_GS, c_clean):
            active_tokens.append('工伤保险')
        if re.search(INS_ALIAS_RES, c_clean):
            active_tokens.append('居民')
        if re.search(INS_ALIAS_EMP, c_clean):
            active_tokens.append('职工')

        if '城镇' in c_clean and not re.search(r'(?:城镇职工|城镇居民)', c_clean):
            has_ambiguous_urban = True
        if any(k in c_clean for k in ('退休', '在职', '已退休', '离休', '无业', '待业', '务农', '学生')) and not any(k in c_clean for k in ('职工', '居民', '公费', '工伤', '自费')):
            has_occupation_only = True

    # 4.2 若整个输入只排除了某种险种，而未提供任何正向有效险种（如“本次未按城乡居民医保结算” / “本次未按职工医保结算”）
    # 强制进入 PENDING 追问，严禁将否定险种作为确定结果输出
    if not active_tokens and negated_tokens:
        neg_str = "、".join(list(dict.fromkeys(negated_tokens)))
        return {
            'valid': False,
            'category': None,
            'status': 'PENDING',
            'reason': 'MISSING_INS_TYPE',
            'pending_reason': 'MISSING_INS_TYPE',
            'message': f"检测到输入排除了【{neg_str}】，但未说明实际参保类型，请明确本次住院按哪类险种结算（城镇职工 / 城乡居民 / 自费）"
        }

    # 4.3 若文本中存在险种间的选择连词（如“职工医保还是居民医保？” / “职工医保或居民医保”）
    if has_ins_choice:
        return {
            'valid': False,
            'category': None,
            'status': 'PENDING',
            'reason': 'AMBIGUOUS_INS_TYPE',
            'pending_reason': 'AMBIGUOUS_INS_TYPE',
            'message': '输入包含选择性险种提问（如“职工医保还是居民医保”），请明确患者本次住院具体按哪类险种结算（城镇职工 / 城乡居民）'
        }

    # 4.4 特殊险种（公费医疗、工伤保险）优先阻断拦截，严禁误判为职工医保
    if '公费医疗' in active_tokens:
        return {
            'valid': False,
            'category': None,
            'status': 'PENDING',
            'reason': 'UNSUPPORTED_INS_TYPE:公费医疗',
            'pending_reason': 'UNSUPPORTED_INS_TYPE:公费医疗',
            'message': '公费医疗统筹区尚未核准 DIP 映射，不适用基本医保 DIP 测算基准，需人工核实'
        }
    if '工伤保险' in active_tokens:
        return {
            'valid': False,
            'category': None,
            'status': 'PENDING',
            'reason': 'UNSUPPORTED_INS_TYPE:工伤保险',
            'pending_reason': 'UNSUPPORTED_INS_TYPE:工伤保险',
            'message': '工伤保险由工伤保险基金单独支付，不适用基本医保 DIP 分值付费测算'
        }

    # 4.5 判定具有基本医保（职工/居民）时的项目自费兼容：
    # 若已有职工或居民医保，且未指定全额纯自费，则整单按职工或居民医保计算，项目自费不拦截整单医保
    has_res = '居民' in active_tokens
    has_emp = '职工' in active_tokens

    if (has_emp or has_res) and not has_pure_self_pay:
        if has_res and has_emp:
            return {
                'valid': False,
                'category': None,
                'status': 'PENDING',
                'reason': 'AMBIGUOUS_INS_TYPE',
                'pending_reason': 'AMBIGUOUS_INS_TYPE',
                'message': '同时存在职工与居民医保且未指明结算口径，请明确患者本次住院按哪类险种结算'
            }
        if has_emp:
            return {'valid': True, 'category': '职工', 'status': 'MATCHED', 'reason': None, 'pending_reason': None, 'message': None}
        if has_res:
            return {'valid': True, 'category': '居民', 'status': 'MATCHED', 'reason': None, 'pending_reason': None, 'message': None}

    # 4.6 纯自费处理
    if '自费' in active_tokens:
        return {
            'valid': True,
            'category': '自费',
            'status': 'NOT_APPLICABLE',
            'reason': 'SELF_PAY',
            'pending_reason': 'SELF_PAY',
            'message': '自费患者不适用医保 DIP 支付规则'
        }

    if has_res and has_emp:
        return {'valid': False, 'category': None, 'status': 'PENDING', 'reason': 'AMBIGUOUS_INS_TYPE', 'pending_reason': 'AMBIGUOUS_INS_TYPE', 'message': '同时存在职工与居民医保且未指明结算口径，请明确患者本次住院按哪类险种结算'}
    if has_res:
        return {'valid': True, 'category': '居民', 'status': 'MATCHED', 'reason': None, 'pending_reason': None, 'message': None}
    if has_emp:
        return {'valid': True, 'category': '职工', 'status': 'MATCHED', 'reason': None, 'pending_reason': None, 'message': None}
    if has_ambiguous_urban:
        return {'valid': False, 'category': None, 'status': 'PENDING', 'reason': 'AMBIGUOUS_INS_TYPE', 'pending_reason': 'AMBIGUOUS_INS_TYPE', 'message': '仅有“城镇”字样含义模糊，需明确是城镇职工还是城镇居民'}
    if has_occupation_only:
        return {'valid': False, 'category': None, 'status': 'PENDING', 'reason': 'OCCUPATION_NOT_INSURANCE_TYPE', 'pending_reason': 'OCCUPATION_NOT_INSURANCE_TYPE', 'message': f'【{s}】属于职业状态而非医保参保类型，请明确为城镇职工医保、城乡居民医保、公费医疗或自费'}

    return {'valid': False, 'category': None, 'status': 'PENDING', 'reason': 'MISSING_INS_TYPE', 'pending_reason': 'MISSING_INS_TYPE', 'message': '缺少有效参保类型'}

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
    if current_cost < Decimal("0"):
        return {
            "light": "无",
            "status": "PENDING",
            "pending_reason": "NEGATIVE_FEE_UNRECONCILED",
            "message": "当前费用为负数（可能存在冲正退费未闭环或结算重算明细），暂停判灯并进入待定核查",
            "ratio_percent": None,
            "boundary_note": None,
            "lower_bound": None,
            "upper_bound": None
        }

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
        # 临界判定：89.95% <= ratio < 90%
        if ratio >= Decimal("89.95") or ratio_display == 90.00:
            boundary_note = "略低于 90%"
    elif cost_val <= upper_limit:
        light = "黄灯"
        status = "预警观察（90%~110%）"
    else:
        light = "红灯"
        status = "费用超标（>110%）"
        # 临界判定：110% < ratio <= 110.05% 或展示四舍五入为 110.00 且原值越界 (如 110.004%)
        if (ratio <= Decimal("110.05") and cost_val > upper_limit) or (ratio_display == 110.00 and cost_val > upper_limit):
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

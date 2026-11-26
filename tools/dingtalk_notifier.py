# -*- coding: utf-8 -*-
"""
tools/dingtalk_notifier.py — 钉钉群机器人 Webhook 主动预警推送模块

严格遵循隐私合规红线与 NOTIFY-DATA-001 / NOTIFY-ACK-001 规范：
1. 出站载荷仅接受受控格式标识（batch_id / rule_version）及经目录/白名单核验的科室标签，
   任何未核验自由文本在构建出站消息前一律丢弃或归并，绝不原样送入出站载荷；
2. 严禁在钉钉消息体中包含任何患者姓名、住院号(visit_id)、床号、诊断编码或医生个人明细；
3. DINGTALK_NOTIFY_ENABLED 禁用时，无论是否显式传入 webhook_url，一律在所有入口跳过发送；
4. 仅当 HTTP 200 且响应体为合法 JSON 且显式包含 errcode == 0 时才判定为 sent=True。
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Set, Tuple

logger = logging.getLogger("hospital_agent.dingtalk_notifier")

DEFAULT_WORKBENCH_URL = "https://hospital-internal.local/inpatient"

# 受控已批准规则版本白名单（仅受控映射值可进入出站载荷）
DEFAULT_ALLOWED_VERSIONS: Set[str] = {"2026_SAMPLE_V1"}

# NOTIFY-DATA-001：院内随机对外通知编号映射表（与内部批次号无任何可公开复算关系，重发复用）
_OUTBOUND_NOTIFY_LOCK = threading.Lock()
_OUTBOUND_NOTIFY_MAP: Dict[str, str] = {}


def _get_or_create_outbound_notify_id(raw_batch: str) -> str:
    if not raw_batch:
        return f"NOTIFY-{secrets.token_hex(4).upper()}"
    with _OUTBOUND_NOTIFY_LOCK:
        existing = _OUTBOUND_NOTIFY_MAP.get(raw_batch)
        if existing is not None:
            return existing
        new_id = f"NOTIFY-{secrets.token_hex(4).upper()}"
        _OUTBOUND_NOTIFY_MAP[raw_batch] = new_id
        return new_id
# 内置标准受控科室白名单（也可由调用方传入 his_ks 目录核验集合）
DEFAULT_ALLOWED_DEPTS: Set[str] = {
    "中医科", "骨伤科", "内科", "外科", "妇科", "儿科",
    "康复医学科", "综合科", "急诊科", "老年病科", "针灸推拿科",
}


def _sign_webhook_url(webhook_url: str, secret: str, timestamp_ms: Optional[int] = None) -> str:
    """按钉钉自定义机器人安全设置（加签）规范生成带 timestamp 和 sign 的 URL。"""
    if not secret:
        return webhook_url
    ts = str(timestamp_ms if timestamp_ms is not None else int(round(time.time() * 1000)))
    string_to_sign = f"{ts}\n{secret}".encode("utf-8")
    hmac_code = hmac.new(secret.encode("utf-8"), string_to_sign, digestmod=hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    sep = "&" if "?" in webhook_url else "?"
    return f"{webhook_url}{sep}timestamp={ts}&sign={sign}"


def _sanitize_outbound_summary(
    summary: Dict[str, Any],
    allowed_depts: Optional[Set[str]] = None,
    allowed_versions: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """
    NOTIFY-DATA-001：独立构建受控出站对象，清洗并拦截任何非受控或未登记版本/自由文本标记；
    对内部批次号使用院内随机映射的独立通知 ID，绝不包含原值或其任何可公开复算摘要。
    """
    raw_batch = str(summary.get("batch_id") or "").strip()
    safe_batch = _get_or_create_outbound_notify_id(raw_batch)

    env_versions = {
        v.strip()
        for v in os.getenv("DINGTALK_ALLOWED_VERSIONS", "").split(",")
        if v.strip()
    }
    ver_whitelist = (allowed_versions if allowed_versions is not None else DEFAULT_ALLOWED_VERSIONS) | env_versions
    raw_ver = str(summary.get("rule_version") or "").strip()
    safe_ver = raw_ver if raw_ver in ver_whitelist else "CONTROLLED_VERSION"

    raw_cutoff = str(summary.get("data_cutoff_time") or "").strip()
    safe_cutoff = raw_cutoff if re.match(r"^\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2}:\d{2})?$", raw_cutoff) else "-"

    whitelist = allowed_depts if allowed_depts is not None else DEFAULT_ALLOWED_DEPTS
    raw_dept_alerts = summary.get("dept_alert_counts") or {}
    safe_depts: Dict[str, Dict[str, int]] = {}
    if isinstance(raw_dept_alerts, dict):
        for dept_key, counts in raw_dept_alerts.items():
            if not isinstance(counts, dict):
                continue
            r_c = max(0, int(counts.get("RED") or 0))
            y_c = max(0, int(counts.get("YELLOW") or 0))
            if r_c == 0 and y_c == 0:
                continue
            d_clean = str(dept_key or "").strip()
            label = d_clean if d_clean in whitelist else "其他已核验科室"
            bucket = safe_depts.setdefault(label, {"RED": 0, "YELLOW": 0})
            bucket["RED"] += r_c
            bucket["YELLOW"] += y_c

    return {
        "batch_id": safe_batch,
        "data_cutoff_time": safe_cutoff,
        "rule_version": safe_ver,
        "total_scanned": max(0, int(summary.get("total_scanned") or 0)),
        "RED": max(0, int(summary.get("RED") or 0)),
        "YELLOW": max(0, int(summary.get("YELLOW") or 0)),
        "GREEN": max(0, int(summary.get("GREEN") or 0)),
        "PENDING": max(0, int(summary.get("PENDING") or 0)),
        "NOT_APPLICABLE": max(0, int(summary.get("NOT_APPLICABLE") or 0)),
        "dept_alert_counts": safe_depts,
    }


def build_alert_markdown(
    summary: Dict[str, Any],
    workbench_url: Optional[str] = None,
    allowed_depts: Optional[Set[str]] = None,
) -> Tuple[str, str]:
    """
    基于受控净化后的出站对象构建钉钉 Markdown 消息 (title, text)。
    """
    safe = _sanitize_outbound_summary(summary, allowed_depts=allowed_depts)
    batch_id = safe["batch_id"]
    cutoff = safe["data_cutoff_time"]
    rule_ver = safe["rule_version"]
    total = safe["total_scanned"]
    red_cnt = safe["RED"]
    yellow_cnt = safe["YELLOW"]
    green_cnt = safe["GREEN"]
    pending_cnt = safe["PENDING"]
    na_cnt = safe["NOT_APPLICABLE"]

    target_url = (workbench_url or os.getenv("WORKBENCH_INTERNAL_URL", DEFAULT_WORKBENCH_URL)).strip()

    if red_cnt > 0:
        title = f"DIP 控费预警：红灯 {red_cnt} 例 / 黄灯 {yellow_cnt} 例"
    elif yellow_cnt > 0:
        title = f"DIP 控费预警：黄灯观察 {yellow_cnt} 例"
    else:
        title = "DIP 在院扫描完成（无红黄灯预警）"

    lines = [
        f"### {title}",
        f"- **扫描批次**：`{batch_id}`",
        f"- **截止时间**：{cutoff}",
        f"- **规则版本**：`{rule_ver}`",
        f"- **汇总统计**：共 {total} 例（红灯 **{red_cnt}** | 黄灯 **{yellow_cnt}** | 绿灯 {green_cnt} | 待定 {pending_cnt} | 自费 {na_cnt}）",
    ]

    dept_alerts = safe["dept_alert_counts"]
    if dept_alerts:
        dept_parts = []
        for dept_name, counts in sorted(dept_alerts.items()):
            r_c = counts["RED"]
            y_c = counts["YELLOW"]
            dept_parts.append(f"{dept_name}(红{r_c}/黄{y_c})")
        lines.append(f"- **预警科室分布**：{', '.join(dept_parts)}")

    lines.append("")
    lines.append(f"> 🔒 **隐私安全提示**：依据院内医疗数据合规要求，患者明细不在移动端群消息展示，请通过院内工作站访问专属 HTTPS 工作台查看：[进入预警看板]({target_url})")
    return title, "\n".join(lines)


def send_dingtalk_alert(
    summary: Dict[str, Any],
    webhook_url: Optional[str] = None,
    secret: Optional[str] = None,
    notify_when_clear: bool = False,
    timeout: int = 8,
    allowed_depts: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """
    发送钉钉群预警通知。
    NOTIFY-ACK-001：禁用开关在所有入口生效；仅合法 JSON 且显式 errcode == 0 才返回 sent=True。
    """
    enabled = os.getenv("DINGTALK_NOTIFY_ENABLED", "true").strip().lower() in ("1", "true", "yes")
    if not enabled:
        return {"sent": False, "skipped": True, "reason": "DISABLED_BY_ENV"}

    url = (webhook_url if webhook_url is not None else os.getenv("DINGTALK_WEBHOOK_URL", "")).strip()
    if not url:
        return {"sent": False, "skipped": True, "reason": "WEBHOOK_URL_NOT_CONFIGURED"}

    red_cnt = int(summary.get("RED") or 0)
    yellow_cnt = int(summary.get("YELLOW") or 0)
    if not notify_when_clear and red_cnt == 0 and yellow_cnt == 0:
        return {"sent": False, "skipped": True, "reason": "NO_RED_OR_YELLOW_ALERTS"}

    sec = (secret if secret is not None else os.getenv("DINGTALK_WEBHOOK_SECRET", "")).strip()
    signed_url = _sign_webhook_url(url, sec)
    title, md_text = build_alert_markdown(summary, allowed_depts=allowed_depts)

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": md_text,
        },
        "at": {"isAtAll": False},
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        signed_url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_raw = resp.read().decode("utf-8", errors="replace")
            try:
                resp_json = json.loads(resp_raw)
            except ValueError:
                logger.warning("EVENT=DINGTALK_ACK_INVALID_JSON status=%s", getattr(resp, "status", "unknown"))
                return {
                    "sent": False,
                    "skipped": False,
                    "reason": "INVALID_JSON_RESPONSE",
                }
            if not isinstance(resp_json, dict) or "errcode" not in resp_json:
                logger.warning("EVENT=DINGTALK_ACK_MISSING_ERRCODE status=%s", getattr(resp, "status", "unknown"))
                return {
                    "sent": False,
                    "skipped": False,
                    "reason": "MISSING_ERRCODE",
                }
            errcode = resp_json.get("errcode")
            # NOTIFY-ACK-001：必须为整数类型的 0，拒绝 bool(False==0)、字符串 "0" 等非整数回执
            ok = (
                resp.status == 200
                and not isinstance(errcode, bool)
                and isinstance(errcode, int)
                and errcode == 0
            )
            if not ok:
                logger.warning("EVENT=DINGTALK_ACK_ERROR status=%s errcode=%s", resp.status, errcode)
            return {
                "sent": ok,
                "skipped": False,
                "reason": "OK" if ok else f"DINGTALK_ERR_{errcode}",
                "response": resp_json,
            }
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        logger.warning("EVENT=DINGTALK_NETWORK_ERROR err_type=%s", type(exc).__name__)
        return {"sent": False, "skipped": False, "reason": f"NETWORK_ERROR:{type(exc).__name__}"}

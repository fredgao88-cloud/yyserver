# -*- coding: utf-8 -*-
"""
tools/dingtalk_notifier.py — 钉钉群机器人 Webhook 主动预警推送模块

严格遵循隐私合规红线：
1. 仅推送批次级脱敏统计（红/黄/绿/待定人数、科室维度的预警计数与工作台入口链接）；
2. 严禁在钉钉消息体中包含任何患者姓名、住院号(visit_id)、床号、诊断编码或医生个人明细；
3. 使用 Python 标准库 urllib / hmac / hashlib 实现，零额外外部依赖；
4. 推送失败或未配置 Webhook 时安全降级记录日志，绝不阻断在院扫描主流程。
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("hospital_agent.dingtalk_notifier")

DEFAULT_WORKBENCH_URL = "https://hospital-internal.local/inpatient"


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


def build_alert_markdown(summary: Dict[str, Any], workbench_url: Optional[str] = None) -> Tuple[str, str]:
    """
    基于扫描摘要构建严格脱敏的钉钉 Markdown 消息 (title, text)。
    仅包含聚合统计数字与科室计数，绝不包含患者级明细。
    """
    batch_id = str(summary.get("batch_id") or "UNKNOWN")
    cutoff = str(summary.get("data_cutoff_time") or "-")
    rule_ver = str(summary.get("rule_version") or "-")
    total = int(summary.get("total_scanned") or 0)
    red_cnt = int(summary.get("RED") or 0)
    yellow_cnt = int(summary.get("YELLOW") or 0)
    green_cnt = int(summary.get("GREEN") or 0)
    pending_cnt = int(summary.get("PENDING") or 0)
    na_cnt = int(summary.get("NOT_APPLICABLE") or 0)

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

    dept_alerts = summary.get("dept_alert_counts") or {}
    if isinstance(dept_alerts, dict) and dept_alerts:
        dept_parts = []
        for dept_name, counts in sorted(dept_alerts.items()):
            r_c = int((counts or {}).get("RED", 0))
            y_c = int((counts or {}).get("YELLOW", 0))
            if r_c > 0 or y_c > 0:
                dept_parts.append(f"{dept_name}(红{r_c}/黄{y_c})")
        if dept_parts:
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
) -> Dict[str, Any]:
    """
    发送钉钉群预警通知。
    返回结构化状态字典 {"sent": bool, "skipped": bool, "reason": str, ...}，绝不抛出未捕获异常。
    """
    enabled = os.getenv("DINGTALK_NOTIFY_ENABLED", "true").strip().lower() in ("1", "true", "yes")
    if not enabled and webhook_url is None:
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
    title, md_text = build_alert_markdown(summary)

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
                resp_json = {"raw": resp_raw}
            ok = (resp.status == 200 and resp_json.get("errcode", 0) == 0)
            if not ok:
                logger.warning("钉钉 Webhook 返回异常: status=%s body=%s", resp.status, resp_json)
            return {
                "sent": ok,
                "skipped": False,
                "reason": "OK" if ok else f"DINGTALK_ERR_{resp_json.get('errcode', resp.status)}",
                "response": resp_json,
            }
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        logger.warning("钉钉 Webhook 推送失败（已隔离，不影响扫描主流程）: %s", exc)
        return {"sent": False, "skipped": False, "reason": f"NETWORK_ERROR: {exc}"}

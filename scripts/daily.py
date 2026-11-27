#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云端每日一次：签到 + 派猫（先领已到家积分，再判断是否可派新一趟）+ 飞书推送。

运行约定：
  * 只在 GitHub Actions 云端 runner 运行；只依赖 Python 标准库。
  * 唯一凭据来源是环境变量 WB_REFRESH_TOKEN（来自仓库 Secret），
    本机登录态与解封密钥永不进入云端。
  * 接口名以本机 app.asar 内实际代码 + 实测请求返回为准（见 README 接口表）。

结论判定（验收标准 4）：
  * 只要签到成功/今日已签，整体退出码为 0；
  * 猫猫部分任何异常都被隔离，只影响推送里的猫猫状态，不影响签到结论。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

# ---------------------------------------------------------------- 配置
REFRESH_API = "https://copilot.tencent.com"
REFRESH_PATH = "/v2/plugin/auth/token/refresh"
BILLING_BASE = "https://www.codebuddy.cn/v2"      # 签到接口基址（实测有效）
TRAVEL_BASE = "https://www.workbuddy.cn"           # 旅行接口独立域名，路径不带 /v2

CHECKIN_STATUS_PATH = "/billing/meter/checkin-activity-status"  # POST
CHECKIN_PATH = "/billing/meter/daily-checkin"                   # POST
TRAVEL_STATUS_PATH = "/activity/growth/buddy/travel/status"      # GET
TRAVEL_BUDDY_INFO_PATH = "/activity/growth/buddy/info"           # GET
TRAVEL_CONFIG_PATH = "/activity/growth/buddy/travel/config"      # GET
TRAVEL_CLAIM_PATH = "/activity/growth/buddy/travel/claim"        # POST
TRAVEL_DEPART_PATH = "/activity/growth/buddy/travel/depart"      # POST

HTTP_TIMEOUT = 20
CST = timezone(timedelta(hours=8))

STATE_TEXT = {
    "idle": "空闲（可派遣）",
    "traveling": "旅行中",
    "arrived": "已到家（待领取）",
}


# ---------------------------------------------------------------- 工具
def now_cst() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def mask(value: str) -> str:
    if not value:
        return "<empty>"
    if len(value) <= 16:
        return "***"
    return value[:6] + "..." + value[-4:]


def redact(obj):
    """递归脱敏，用于日志与证据输出；token 明文绝不外泄。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if re.search(r"token|secret|sign|authorization|cookie", str(k), re.I):
                out[k] = "<redacted>" if v else v
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    if isinstance(obj, str):
        # JWT 或长 base64 串一律脱敏
        if re.search(r"eyJ[A-Za-z0-9_\-]{10,}", obj) or len(obj) > 300:
            return "<redacted:string len=%d>" % len(obj)
        return obj
    return obj


def log(msg: str) -> None:
    print("[%s] %s" % (now_cst(), msg), flush=True)


def api_call(base: str, path: str, token: str, method: str = "POST",
             payload=None) -> tuple[int, dict]:
    url = base.rstrip("/") + path
    body = None
    if method == "POST":
        body = json.dumps(payload if payload is not None else {},
                          ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, json.loads(raw)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            return exc.code, {"code": -1, "msg": "HTTP %s" % exc.code}
    except Exception as exc:  # noqa: BLE001
        return 0, {"code": -1, "msg": type(exc).__name__ + ": " + str(exc)[:160]}


def token_days_left(token: str):
    """从 JWT 的 exp 字段解析剩余天数；非 JWT 或解析失败返回 None。
    仅读取过期时间，不打印任何 token 内容。"""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        obj = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8", "ignore"))
        exp = obj.get("exp")
        if not exp:
            return None
        return (float(exp) - time.time()) / 86400.0
    except Exception:  # noqa: BLE001
        return None


def refresh_access_token(rt: str) -> str:
    # 记录 refresh token 剩余有效期，供每天推送里做"提前预警"
    days = token_days_left(rt)
    if days is not None:
        os.environ["WB_TOKEN_DAYS"] = ("%.1f" % days)
        if days < 0:
            log("⚠️ refresh token 已过期（%.1f 天），需立刻重新导出" % days)
        elif days <= 7:
            log("⚠️ refresh token 仅剩 %.1f 天，请尽快重新导出更新 Secret" % days)
    req = urllib.request.Request(REFRESH_API + REFRESH_PATH, data=b"{}", method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Refresh-Token", rt)
    req.add_header("X-Auth-Refresh-Source", "plugin")
    req.add_header("X-Domain", "copilot.tencent.com")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("refresh token 换取 access token 失败：%s" % str(exc)[:160])
    if body.get("code") != 0 or not isinstance(body.get("data"), dict):
        raise RuntimeError("refresh token 刷新失败：code=%s msg=%s（请在本机重新导出 Secret）"
                           % (body.get("code"), body.get("msg")))
    at = body["data"].get("accessToken") or ""
    if not at:
        raise RuntimeError("refresh 应答缺少 accessToken")
    log("access token 已刷新（%s）" % mask(at))
    return at


def evidence_add(evidence: dict, name: str, status: int, body) -> None:
    """保存脱敏后的原始返回，供验收核对。"""
    evidence["apis"][name] = {
        "http_status": status,
        "raw_response_redacted": redact(body),
    }


# ---------------------------------------------------------------- 签到
def do_checkin(at: str, evidence: dict) -> dict:
    result = {"success": False, "action": None, "msg": "", "detail": {}}

    st, body = api_call(BILLING_BASE, CHECKIN_STATUS_PATH, at, method="POST")
    evidence_add(evidence, "checkin_status", st, body)
    if st != 200 or body.get("code") != 0:
        result["msg"] = "查询签到状态失败：HTTP %s / %s" % (st, body.get("msg"))
        return result
    data = body.get("data") or {}
    result["detail"] = {
        "theme": data.get("theme_name"), "season": data.get("season"),
        "streak_days": data.get("streak_days"), "total_credits": data.get("total_credits"),
        "today_checked_in": data.get("today_checked_in"),
    }
    if data.get("today_checked_in"):
        result.update(success=True, action="skipped", msg="今日已签到，无需重复打卡")
        return result

    st, body = api_call(BILLING_BASE, CHECKIN_PATH, at, method="POST")
    evidence_add(evidence, "daily_checkin", st, body)
    code, msg = body.get("code"), body.get("msg") or ""
    if st == 200 and code == 0:
        got = (body.get("data") or {}).get("credit")
        result.update(success=True, action="checked_in",
                      msg=msg or "签到成功", points=got)
    elif code == 10001 and "已签到" in msg:
        result.update(success=True, action="skipped", msg=msg or "今日已签到")
    else:
        result["msg"] = "签到失败：HTTP %s / code=%s / %s" % (st, code, msg)
    return result


# ---------------------------------------------------------------- 派猫
def travel_get(at: str, path: str, evidence: dict, name: str):
    st, body = api_call(TRAVEL_BASE, path, at, method="GET")
    evidence_add(evidence, name, st, body)
    if st != 200 or body.get("code") != 0:
        return None
    return body.get("data")


def travel_post(at: str, path: str, payload: dict, evidence: dict, name: str):
    st, body = api_call(TRAVEL_BASE, path, at, method="POST", payload=payload)
    evidence_add(evidence, name, st, body)
    if st == 200 and body.get("code") == 0:
        return True, body.get("data") or {}, ""
    return False, {}, body.get("msg") or ("HTTP %s" % st)


def do_travel(at: str, evidence: dict, location_id=None) -> dict:
    """先领已到家积分，再判断今日能否派新一趟；已派则跳过。

    本函数内部吞掉所有异常，返回一个可供展示的 dict，绝不向上抛。
    """
    out = {"ok": False, "state": None, "state_text": None, "location": None,
           "reward": None, "remaining": None, "actions": [], "error": None}
    try:
        status = travel_get(at, TRAVEL_STATUS_PATH, evidence, "travel_status_before")
        if status is None:
            out["error"] = "查询旅行状态失败"
            return out

        # 步骤 1：领取已经到家的旅行积分
        if status.get("state") == "arrived":
            ok, data, msg = travel_post(at, TRAVEL_CLAIM_PATH, {}, evidence, "travel_claim")
            if ok:
                credit = data.get("reward_credit")
                out["reward"] = credit
                out["actions"].append("已领取到家旅行积分 +%s" % (credit if credit is not None else "?"))
            else:
                out["actions"].append("领取旅行积分失败：%s" % msg)
            # 领取后重新读取状态，作为是否可派的最新依据
            fresh = travel_get(at, TRAVEL_STATUS_PATH, evidence, "travel_status_after_claim")
            if fresh is not None:
                status = fresh

        out["state"] = status.get("state")
        out["state_text"] = STATE_TEXT.get(status.get("state"), status.get("state"))
        out["reward"] = out["reward"] if out["reward"] is not None else status.get("reward_credit")
        loc = status.get("location")
        out["location"] = loc.get("name") if isinstance(loc, dict) else None
        if status.get("state") == "traveling":
            arrive, now = status.get("arrive_at"), status.get("server_now")
            if isinstance(arrive, int) and isinstance(now, int):
                mins = max(0, (arrive - now) // 60)
                out["remaining"] = "%d 小时 %d 分" % divmod(mins, 60)

        # 步骤 2：判断今天还能不能派新的一趟
        if status.get("daily_limit_reached"):
            out["actions"].append("今日已派过猫猫，跳过派遣")
        elif status.get("state") in (None, "", "idle"):
            config = travel_get(at, TRAVEL_CONFIG_PATH, evidence, "travel_config") or {}
            locations = [x for x in (config.get("locations") or []) if isinstance(x, dict)]
            chosen = location_id
            if chosen is None and locations:
                chosen = random.choice([x.get("id") for x in locations if x.get("id") is not None])
            if chosen is None:
                out["actions"].append("无可用派遣地点，跳过派遣")
            else:
                ok, data, msg = travel_post(
                    at, TRAVEL_DEPART_PATH, {"location_id": chosen}, evidence, "travel_depart")
                if ok:
                    dloc = data.get("location") or {}
                    dname = dloc.get("name") if isinstance(dloc, dict) else None
                    out["location"] = dname or out["location"]
                    out["state"] = "traveling"
                    out["state_text"] = STATE_TEXT["traveling"]
                    out["actions"].append("已派出猫猫前往【%s】" % (dname or ("地点 %s" % chosen)))
                elif "no active buddy" in (msg or "").lower():
                    # 账号尚未拥有 Buddy：属于前置条件未满足，不是脚本故障
                    info = travel_get(at, TRAVEL_BUDDY_INFO_PATH, evidence, "travel_buddy_info")
                    has_buddy = bool(info and info.get("buddy"))
                    out["actions"].append(
                        "账号暂无可用猫猫（no active buddy），跳过派遣；"
                        "请先在 WorkBuddy 成长计划中激活 Buddy 后再派。" if not has_buddy
                        else "派出猫猫失败：%s" % msg)
                else:
                    out["actions"].append("派出猫猫失败：%s" % msg)
        else:
            out["actions"].append("猫猫仍在旅行中，跳过派遣")

        final = travel_get(at, TRAVEL_STATUS_PATH, evidence, "travel_status_final")
        if final:
            out["state"] = final.get("state")
            out["state_text"] = STATE_TEXT.get(final.get("state"), final.get("state"))
        out["ok"] = True
    except Exception as exc:  # noqa: BLE001 - 猫猫失败不影响签到结论
        out["error"] = type(exc).__name__ + ": " + str(exc)[:200]
        out["actions"].append("猫猫环节异常，已隔离：" + out["error"])
    return out


# ---------------------------------------------------------------- 飞书推送
def feishu_sign(secret: str, timestamp: int) -> str:
    string_to_sign = "%s\n%s" % (timestamp, secret)
    hmac_code = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


def push_feishu(checkin: dict, cat: dict, ok: bool) -> tuple[bool, str]:
    webhook = os.environ.get("FEISHU_WEBHOOK", "").strip()
    if not webhook:
        return False, "未配置 FEISHU_WEBHOOK，跳过推送"

    title = "Buddy 加油站日报 · %s" % now_cst()
    ck_icon = "✅" if checkin.get("success") else "❌"
    ck_lines = [
        "**结论**：%s %s" % (ck_icon, "成功" if checkin.get("success") else "失败"),
        "**动作**：%s" % (checkin.get("action") or "无"),
        "**说明**：%s" % (checkin.get("msg") or "无"),
    ]
    d = checkin.get("detail") or {}
    if d:
        ck_lines.append("**连签**：%s 天　**本期累计**：%s 分" % (
            d.get("streak_days"), d.get("total_credits")))

    cat_icon = "🐱" if cat.get("ok") else "⚠️"
    cat_lines = ["**状态**：%s %s" % (cat_icon, cat.get("state_text") or cat.get("state") or "未知")]
    if cat.get("location"):
        cat_lines.append("**地点**：%s" % cat["location"])
    if cat.get("reward") is not None:
        cat_lines.append("**旅行奖励**：+%s 分" % cat["reward"])
    if cat.get("remaining"):
        cat_lines.append("**预计剩余**：%s" % cat["remaining"])
    for action in cat.get("actions") or []:
        cat_lines.append("- %s" % action)

    elements = [
        {"tag": "markdown", "content": "### ① 签到结果\n" + "\n".join(ck_lines)},
        {"tag": "hr"},
        {"tag": "markdown", "content": "### ② 猫猫状态\n" + "\n".join(cat_lines)},
    ]

    # ③ 凭证预警：refresh token 剩余不足 7 天时提醒更新 Secret
    days_raw = os.environ.get("WB_TOKEN_DAYS", "")
    if days_raw:
        try:
            days = float(days_raw)
            if days <= 7:
                warn = ("🔴 **凭证已过期 %.1f 天**，签到即将失败！" % abs(days) if days < 0
                        else "🟠 **凭证仅剩 %.1f 天**" % days)
                elements.append({"tag": "hr"})
                elements.append({"tag": "markdown", "content":
                    "### ③ ⚠️ 凭证预警\n" + warn +
                    "\n请在本机重新导出 token 并更新 GitHub Secret `WB_REFRESH_TOKEN`"
                    "\n（步骤见备份包里的《恢复说明》第二节）"})
        except ValueError:
            pass

    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": title},
                       "template": "green" if ok else "red"},
            "elements": elements,
        },
    }

    secret = os.environ.get("FEISHU_SECRET", "").strip()
    if secret:
        ts = int(time.time())
        card["timestamp"] = str(ts)
        card["sign"] = feishu_sign(secret, ts)

    data = json.dumps(card, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(webhook, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
        # 飞书成功返回 code=0 或 StatusCode=0
        if body.get("code") in (0, None) and body.get("StatusCode", 0) == 0:
            return True, "飞书推送成功"
        return False, "飞书返回：%s" % json.dumps(redact(body), ensure_ascii=False)[:300]
    except Exception as exc:  # noqa: BLE001 - 推送失败不影响签到结论
        return False, "飞书推送失败：%s" % str(exc)[:200]


# ---------------------------------------------------------------- 主流程
def main() -> int:
    rt = os.environ.get("WB_REFRESH_TOKEN", "").strip()
    if not rt:
        log("缺少环境变量 WB_REFRESH_TOKEN（需在本机导出后写入仓库 Secret）")
        return 2

    evidence = {"run_at": now_cst(), "apis": {}, "summary": {}}

    try:
        at = refresh_access_token(rt)
    except Exception as exc:  # noqa: BLE001
        log(str(exc))
        evidence["summary"] = {"fatal": str(exc)[:300]}
        _write_evidence(evidence)
        return 1

    checkin = do_checkin(at, evidence)
    log("签到：%s / %s" % (checkin.get("action"), checkin.get("msg")))

    try:
        cat = do_travel(at, evidence, os.environ.get("WB_LOCATION_ID", "").strip() or None)
    except Exception as exc:  # noqa: BLE001 - 双保险
        cat = {"ok": False, "state": None, "actions": [], "error": str(exc)[:200]}
    log("猫猫：%s / %s" % (cat.get("state_text"), "; ".join(cat.get("actions") or [])))

    ok = bool(checkin.get("success"))   # 验收标准 4：签到成功即整体成功
    pushed, push_msg = push_feishu(checkin, cat, ok)
    log(push_msg)

    evidence["summary"] = {
        "overall_success": ok,
        "checkin": redact(checkin),
        "cat": redact(cat),
        "feishu_push": {"ok": pushed, "msg": push_msg},
    }
    _write_evidence(evidence)

    log("=== 本次运行结束：%s ===" % ("成功" if ok else "失败"))
    return 0 if ok else 1


def _write_evidence(evidence: dict) -> None:
    path = os.environ.get("EVIDENCE_PATH", "")
    if not path and os.environ.get("GITHUB_ACTIONS"):
        # 云端默认落一份脱敏证据供 artifact 上传
        path = os.path.join("evidence", "latest.json")
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(evidence, f, ensure_ascii=False, indent=2)
        log("脱敏证据已写入：%s" % path)
    except Exception as exc:  # noqa: BLE001
        log("写入证据失败：%s" % str(exc)[:120])


if __name__ == "__main__":
    raise SystemExit(main())

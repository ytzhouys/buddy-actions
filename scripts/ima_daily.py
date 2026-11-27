#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ima「每日登录福利」自动签到

用法:
    python ima_checkin.py            执行签到
    python ima_checkin.py --dry-run  只查询状态, 不签到
    python ima_checkin.py --json     机器可读的一行 JSON 输出

凭证从环境变量 IMA_CREDENTIALS 读取(JSON 字符串, 来自 GitHub Secret),
不落盘、不进日志。仅使用 Python 标准库。

飞书推送: 设置 FEISHU_WEBHOOK (可选 FEISHU_SECRET) 后自动推送签到卡片。
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_URL = "https://ima.qq.com"
REFRESH_URL = BASE_URL + "/cgi-bin/auth_login/refresh"
ACTIVITY_INFO_URL = BASE_URL + "/cgi-bin/daily_login_activity/get_activity_info"
CHECK_IN_URL = BASE_URL + "/cgi-bin/daily_login_activity/check_in"

# 固定按北京时间判断"今天/星期几"，不依赖机器或 CI 容器的系统时区。
# 中国无夏令时，固定 UTC+8 即可，无需 tzdata。
CST = timezone(timedelta(hours=8))


def now_cst():
    return datetime.now(CST)

# 与 ima 内置浏览器一致的固定请求头(服务器不严格校验具体值)
FROM_BROWSER_IMA = "1"
EXTENSION_VERSION = "5.17.2"

CHECKIN_TYPE_DAILY = 1
CHECKIN_TYPE_WEEKLY_BONUS = 2

# 服务端状态枚举(来自页面 JS): 1=已过期 2=已领取 3=可领取 4=未解锁
STATUS_CLAIMED = 2
STATUS_AVAILABLE = 3
STATUS_LOCKED = 4
STATUS_TEXT = {0: "未知", 1: "已过期", 2: "已领取", 3: "可领取", 4: "未解锁"}

BONUS_TITLE = "满签奖励"
# 满签奖励在周日结算发放: 一周签到从周一开始, 第 7 天正好是周日
BONUS_WEEKDAY = 6  # datetime.weekday(): 周一=0 ... 周日=6

# 服务端业务码(实测 2026-10-19):
#   1001 = week not fully claimed, dayN unclaimed  未满签
#   1002 = today already checked in                今天已签过
# 注意: 同一接口短时间内被拒多次后服务端直接回 HTTP 403 + 空 body,
# 所以调用失败必须能接住, 不能让异常冒出去打断签到。
CODE_OK = 0
CODE_NOT_FULL_WEEK = 1001
CODE_ALREADY_CHECKED_IN = 1002

CRED_FILE = Path(__file__).resolve().with_name("credentials.json")


def _to_int32(value):
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value >= 0x80000000 else value


def bkn(token):
    """复刻页面 JS 的 djb2 签名算法(l_ 函数), 输出 x-ima-bkn 头。"""
    result = 5381
    for ch in token:
        result += _to_int32(_to_int32(result) << 5) + ord(ch)
    return _to_int32(result) & 0x7FFFFFFF


def http_post_json(url, body, headers, timeout=20):
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        status = exc.code

    # 服务端被连续拒绝后会返回 403 + 空 body, 这里统一转成可接住的结构,
    # 不再抛异常——否则一次"试领失败"就能把整个签到任务打断。
    if not raw.strip():
        return {
            "code": -1,
            "msg": "HTTP %s 空响应(可能被限流, 稍后重试)" % status,
            "http_status": status,
        }
    try:
        data = json.loads(raw)
    except ValueError:
        return {
            "code": -1,
            "msg": "HTTP %s 非 JSON 响应: %s" % (status, raw[:200]),
            "http_status": status,
        }
    if isinstance(data, dict):
        data.setdefault("http_status", status)
    return data


def load_credentials():
    """从环境变量读取凭证 (GitHub Secret), 不落盘。"""
    raw = os.environ.get("IMA_CREDENTIALS", "")
    if not raw:
        sys.exit("[错误] 未设置环境变量 IMA_CREDENTIALS。请在仓库 Secret 中配置。")
    try:
        cred = json.loads(raw)
    except Exception:
        sys.exit("[错误] IMA_CREDENTIALS 不是合法 JSON。")
    for key in ("ima_uid", "uid_type", "ima_guid", "refresh_token"):
        if not cred.get(key):
            sys.exit("[错误] IMA_CREDENTIALS 缺少字段 %r, 请重新扫码授权。" % key)
    return cred


def mask(value):
    s = str(value)
    if len(s) <= 8:
        return "*" * len(s)
    return s[:4] + "..." + s[-4:]


def feishu_sign(secret, timestamp):
    string_to_sign = "%s\n%s" % (timestamp, secret)
    hmac_code = hmac.new(
        string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


def push_feishu(result):
    """推送一张 ima 签到卡片到飞书。失败不抛异常, 不影响签到结论。"""
    webhook = os.environ.get("FEISHU_WEBHOOK", "")
    if not webhook:
        print("[提示] 未设置 FEISHU_WEBHOOK, 跳过推送。")
        return False
    now = now_cst().strftime("%Y-%m-%d %H:%M")
    days = result.get("checkin_days", "?")
    total = result.get("total_reward_points", "?")
    performed = result.get("checkin_performed")
    msg = result.get("message", "")
    if performed:
        head = "✅ 今日签到成功"
    elif "已签到" in msg:
        head = "⏭️ 今日已签到（跳过）"
    else:
        head = "⚠️ 请检查签到状态"
    lines = [
        "**ima 每日算力签到**",
        "",
        "**结果**：%s" % head,
        "**本周进度**：%s" % result.get("week_summary", "-"),
        "**累计**：已签到 %s 天，共 %s 算力" % (days, total),
        "**时间**：%s" % now,
    ]
    age = result.get("cred_age_days")
    if age is not None and age >= 45:
        lines.append("")
        lines.append("🟠 **凭证已用 %d 天**，建议重新微信扫码并更新 Secret `IMA_CREDENTIALS`"
                     % int(age))
    card = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": "ima 算力签到"},
                       "template": "green" if performed else "blue"},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}],
        },
    }
    body = dict(card)
    secret = os.environ.get("FEISHU_SECRET", "")
    if secret:
        ts = str(int(time.time()))
        body["timestamp"] = ts
        body["sign"] = feishu_sign(secret, ts)
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(webhook, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            r = json.loads(resp.read().decode("utf-8", "ignore"))
        if r.get("code") == 0 or r.get("StatusCode") == 0:
            print("飞书推送成功")
            return True
        print("飞书推送返回异常: code=%s" % r.get("code"))
    except Exception as exc:
        print("飞书推送失败: %s" % type(exc).__name__)
    return False


def build_cookie(cred, token):
    parts = [
        "IMA-UID=%s" % cred["ima_uid"],
        "IMA-TOKEN=%s" % token,
        "IMA-REFRESH-TOKEN=%s" % cred["refresh_token"],
        "UID-TYPE=%s" % cred["uid_type"],
        "IMA-GUID=%s" % cred["ima_guid"],
        "PLATFORM=H5",
    ]
    return "; ".join(parts)


def auth_headers(cookie, token=None):
    headers = {
        "Content-Type": "application/json",
        "from_browser_ima": FROM_BROWSER_IMA,
        "extension_version": EXTENSION_VERSION,
        "x-ima-cookie": cookie,
    }
    if token:
        headers["x-ima-bkn"] = str(bkn(token))
    return headers


def refresh_access_token(cred):
    """用长期 refresh_token 换 2 小时有效的 access token。"""
    device_cookie = "IMA-GUID=%s; PLATFORM=H5" % cred["ima_guid"]
    body = {
        "userId": cred["ima_uid"],
        "refreshToken": cred["refresh_token"],
        "tokenType": -1,
    }
    data = http_post_json(REFRESH_URL, body, auth_headers(device_cookie))
    token = data.get("token")
    if data.get("code") != 0 or not token:
        sys.exit(
            "[错误] 刷新登录失败: code=%s msg=%s\n"
            "refresh_token 可能已失效。请完全退出 ima 后运行 extract_credentials.py 重新提取。"
            % (data.get("code"), data.get("msg"))
        )
    return token


def get_activity_info(cookie, token):
    return http_post_json(ACTIVITY_INFO_URL, {}, auth_headers(cookie, token))


def check_in(cookie, token, checkin_type):
    return http_post_json(CHECK_IN_URL, {"type": checkin_type}, auth_headers(cookie, token))


def find_item(info, top):
    for item in info.get("infos") or []:
        if item.get("top") == top:
            return item
    return None


def reward_of(resp):
    """签到接口的奖励字段是 snake_case 的 reward_received。"""
    if not isinstance(resp, dict):
        return None
    return resp.get("reward_received", resp.get("rewardReceived"))


def week_summary(info):
    cells = []
    for item in info.get("infos") or []:
        top = item.get("top") or "?"
        status = item.get("status")
        mark = {2: "已领", 3: "可领", 4: "未解锁"}.get(status, STATUS_TEXT.get(status, "?"))
        cells.append("%s:%s" % (top, mark))
    return "  ".join(cells)


def main():
    parser = argparse.ArgumentParser(description="ima 每日登录福利自动签到")
    parser.add_argument("--dry-run", action="store_true", help="只查询状态, 不签到")
    parser.add_argument("--json", dest="as_json", action="store_true", help="输出一行 JSON")
    args = parser.parse_args()

    now = now_cst().strftime("%Y-%m-%d %H:%M:%S")
    cred = load_credentials()

    token = refresh_access_token(cred)
    cookie = build_cookie(cred, token)

    info = get_activity_info(cookie, token)
    if info.get("code") != 0:
        sys.exit("[错误] 查询活动失败: code=%s msg=%s" % (info.get("code"), info.get("msg")))

    today = find_item(info, "今日")
    if today is None:
        sys.exit("[错误] 未找到今日条目, 活动可能已改版, 请检查。")

    # 凭证年龄预警：从 saved_at 算已用天数（凭证无 exp 字段，只能按保存时间估算）
    cred_age = None
    saved = cred.get("saved_at")
    if saved:
        try:
            from datetime import datetime as _dt
            cred_age = (now_cst() - _dt.strptime(saved, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)).days
            if cred_age >= 45:
                print("[预警] ima 凭证已用 %d 天，建议重新扫码更新 Secret" % cred_age)
        except Exception:
            cred_age = None

    result = {
        "time": now,
        "today_status": today.get("status"),
        "cred_age_days": cred_age,
        "dry_run": args.dry_run,
        "checkin_performed": False,
        "checkin_result": None,
        "weekly_bonus_status": None,
        "weekly_bonus_claimed": False,
        "checkin_days": info.get("checkin_days"),
        "total_reward_points": info.get("total_reward_points"),
    }
    messages = []

    # ---------- 1) 日常签到 ----------
    if today.get("status") == STATUS_AVAILABLE:
        if args.dry_run:
            messages.append("今天可签到 (dry-run, 未执行)")
        else:
            resp = check_in(cookie, token, CHECKIN_TYPE_DAILY)
            result["checkin_performed"] = True
            result["checkin_result"] = resp
            if resp.get("code") == 0:
                got = reward_of(resp)
                messages.append("签到成功, 获得 %s算力" % got if got else "签到成功")
            else:
                messages.append(
                    "签到返回 code=%s msg=%s" % (resp.get("code"), resp.get("msg"))
                )
            # 第 7 天(周日)日签会在同一次请求里解锁「满签奖励」。
            # 必须重新拉状态, 否则下面读到的是签到前的旧快照, 满签奖励还是「未解锁」。
            info = get_activity_info(cookie, token)
    elif today.get("status") == STATUS_CLAIMED:
        messages.append("今天已签到")
    else:
        messages.append("今天不可签到 (status=%s)" % today.get("status"))

    # ---------- 2) 满签奖励: 只有周日(第 7 天)签到完才试领一次 ----------
    # 签到周期周一~周日, 满签奖励周日结算。非周日静默跳过: 不发请求、不打日志。
    # 周日照常发一次 type=2, 并把服务端返回码全部接住。
    bonus = find_item(info, BONUS_TITLE)
    if bonus is not None:
        result["weekly_bonus_status"] = bonus.get("status")

    if now_cst().weekday() != BONUS_WEEKDAY:
        pass  # 非周日: 静默跳过
    elif args.dry_run:
        if bonus is None:
            messages.append("满签奖励: 接口未返回该条目, 活动可能已改版")
        elif bonus.get("status") == STATUS_CLAIMED:
            messages.append("满签奖励本期已领取")
        elif bonus.get("status") == STATUS_AVAILABLE:
            messages.append("满签奖励可领取 (dry-run, 未执行)")
        else:
            messages.append(
                "满签奖励未解锁 (status=%s, 当前 %s 天)"
                % (bonus.get("status"), info.get("checkin_days"))
            )
    else:
        resp = check_in(cookie, token, CHECKIN_TYPE_WEEKLY_BONUS)
        result["weekly_bonus_result"] = resp
        code = resp.get("code")
        if code == CODE_OK:
            got = reward_of(resp)
            result["weekly_bonus_claimed"] = True
            result["weekly_bonus_reward"] = got
            messages.append("满签奖励已领取%s" % (" +%s算力" % got if got else ""))
            info = get_activity_info(cookie, token)  # 刷新最终总数
        elif code == CODE_NOT_FULL_WEEK:
            # 周日但本周没签满, 属于正常情况
            result["weekly_bonus_reason"] = "not_full_week"
            messages.append(
                "满签奖励未到条件 (%s, 当前 %s 天)"
                % (resp.get("msg"), info.get("checkin_days"))
            )
        elif code == CODE_ALREADY_CHECKED_IN:
            result["weekly_bonus_reason"] = "already_checked_in"
            messages.append("满签奖励: 今天已签到, 无需重复")
        else:
            # 含 -1(HTTP 403/空响应/限流) 等未知情况: 只记录, 不影响签到结果
            result["weekly_bonus_reason"] = "error"
            messages.append(
                "满签奖励试领异常 code=%s msg=%s" % (code, resp.get("msg"))
            )

    # ---------- 3) 汇总最终状态 ----------
    result["checkin_days"] = info.get("checkin_days")
    result["total_reward_points"] = info.get("total_reward_points")
    result["week_summary"] = week_summary(info)
    result["message"] = " | ".join(messages) if messages else "无可执行动作"

    # 打印脱敏摘要 (不含任何 token)
    print("[ima 每日签到] %s" % now)
    print("本周进度: %s" % result["week_summary"])
    print("累计: 已签到 %s 天, 共 %s 算力" % (result["checkin_days"], result["total_reward_points"]))
    for line in messages:
        print(line)

    if not args.dry_run:
        push_feishu(result)

    if args.as_json:
        print("__IMA_RESULT__" + json.dumps(result, ensure_ascii=False))
    return result


if __name__ == "__main__":
    main()

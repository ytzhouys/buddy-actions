#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""微信 / 钉钉推送助手（只依赖 Python 标准库）。

设计原则：
  * 所有 webhook / token 只从环境变量读取，绝不打印、绝不入库。
  * 任一渠道失败都被隔离，只返回 (ok, msg)，不影响签到结论与退出码。
  * 未配置的环境变量静默跳过，方便按需启用单个渠道。

支持的渠道：
  1) 企业微信群机器人  WECHAT_WEBHOOK        （推荐，和飞书一样建群加机器人）
  2) Server 酱         SERVERCHAN_KEY        （推到个人微信，可选）
  3) PushPlus          PUSHPLUS_TOKEN        （推到个人微信，可选）
  4) 钉钉群机器人      DINGTALK_WEBHOOK      + 可选 DINGTALK_SECRET（加签）

消息正文统一用 markdown；各平台对 markdown 支持略有差异，这里用三者通用语法。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

HTTP_TIMEOUT = 20


# ---------------------------------------------------------------- 工具
def _post(url: str, payload: dict) -> tuple[int, dict]:
    """POST JSON，返回 (http_status, body)。异常被转成结构，不抛出。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, _as_json(raw)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, _as_json(exc.read().decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            return exc.code, {"code": -1, "msg": "HTTP %s" % exc.code}
    except Exception as exc:  # noqa: BLE001
        return 0, {"code": -1, "msg": type(exc).__name__ + ": " + str(exc)[:120]}


def _as_json(raw: str) -> dict:
    if not raw.strip():
        return {"code": -1, "msg": "empty response"}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {"code": -1, "msg": "non-object response"}
    except ValueError:
        return {"code": -1, "msg": "non-json response: " + raw[:100]}


# ---------------------------------------------------------------- 企业微信群机器人
def push_wecom(webhook: str, title: str, md: str) -> tuple[bool, str]:
    """企业微信群机器人：返回 errcode=0 即成功。"""
    # markdown 内容前面加一行加粗标题，企业微信卡片列表里更易识别
    content = "**%s**\n\n%s" % (title, md)
    st, body = _post(webhook, {"msgtype": "markdown", "markdown": {"content": content}})
    if st == 200 and body.get("errcode") == 0:
        return True, "企业微信推送成功"
    return False, "企业微信返回：HTTP %s / %s" % (st, json.dumps(body, ensure_ascii=False)[:200])


# ---------------------------------------------------------------- Server 酱（个人微信）
def push_serverchan(key: str, title: str, md: str) -> tuple[bool, str]:
    """Server 酱 Turbo：https://sctapi.ftqq.com/<SENDKEY>.send"""
    url = "https://sctapi.ftqq.com/%s.send" % urllib.parse.quote(key, safe="")
    st, body = _post(url, {"title": title, "desp": md})
    # 成功返回 code=0；有些老版本返回 data.code=0
    code = body.get("code")
    if code is None and isinstance(body.get("data"), dict):
        code = body["data"].get("code")
    if st == 200 and code == 0:
        return True, "Server 酱推送成功"
    return False, "Server 酱返回：HTTP %s / %s" % (st, json.dumps(body, ensure_ascii=False)[:200])


# ---------------------------------------------------------------- PushPlus（个人微信）
def push_pushplus(token: str, title: str, md: str) -> tuple[bool, str]:
    """PushPlus：https://www.pushplus.plus/send"""
    st, body = _post("https://www.pushplus.plus/send", {
        "token": token, "title": title, "content": md, "template": "markdown",
    })
    if st == 200 and body.get("code") == 200:
        return True, "PushPlus 推送成功"
    return False, "PushPlus 返回：HTTP %s / %s" % (st, json.dumps(body, ensure_ascii=False)[:200])


# ---------------------------------------------------------------- 钉钉群机器人
def dingtalk_sign(secret: str, timestamp_ms: int) -> str:
    """钉钉加签：HMAC-SHA256(secret, timestamp + '\\n' + secret)，URL 编码后作 sign。"""
    string_to_sign = "%s\n%s" % (timestamp_ms, secret)
    digest = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"),
                      digestmod=hashlib.sha256).digest()
    return urllib.parse.quote_plus(base64.b64encode(digest).decode("utf-8"))


def push_dingtalk(webhook: str, secret: str, title: str, md: str) -> tuple[bool, str]:
    url = webhook
    if secret:
        ts = int(time.time() * 1000)
        sep = "&" if "?" in url else "?"
        url = "%s%stimestamp=%d&sign=%s" % (url, sep, ts, dingtalk_sign(secret, ts))
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": "### %s\n\n%s" % (title, md)},
    }
    st, body = _post(url, payload)
    if st == 200 and body.get("errcode") == 0:
        return True, "钉钉推送成功"
    return False, "钉钉返回：HTTP %s / %s" % (st, json.dumps(body, ensure_ascii=False)[:200])


# ---------------------------------------------------------------- 正文拼装
def compose_md(title: str, sections: list[tuple[str, list[str]]]) -> str:
    """把多个 (小标题, 行列表) 拼成各平台通用 markdown。

    title 只作为消息标题传给各平台 API，正文里也放一份便于在卡片列表识别。
    """
    parts = ["**%s**" % title, ""]
    for heading, lines in sections:
        parts.append("### %s" % heading)
        parts.extend(lines)
        parts.append("")
    return "\n".join(parts).rstrip()


# ---------------------------------------------------------------- 统一入口
def push_all(title: str, md: str) -> dict:
    """按已配置的 Secret 推送到所有渠道；未配置的跳过，失败不抛出。

    返回 {"渠道名": {"ok": bool, "msg": str}, ...}，供写进证据与日志。
    """
    results: dict[str, dict] = {}

    def run(name: str, fn, *args) -> None:
        try:
            ok, msg = fn(*args)
        except Exception as exc:  # noqa: BLE001 - 推送失败绝不影响签到
            ok, msg = False, "%s异常：%s" % (name, type(exc).__name__)
        results[name] = {"ok": ok, "msg": msg}

    wecom = os.environ.get("WECHAT_WEBHOOK", "").strip()
    if wecom:
        run("企业微信", push_wecom, wecom, title, md)

    sc = os.environ.get("SERVERCHAN_KEY", "").strip()
    if sc:
        run("Server酱", push_serverchan, sc, title, md)

    pp = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    if pp:
        run("PushPlus", push_pushplus, pp, title, md)

    ding = os.environ.get("DINGTALK_WEBHOOK", "").strip()
    if ding:
        run("钉钉", push_dingtalk, ding, os.environ.get("DINGTALK_SECRET", "").strip(), title, md)

    return results

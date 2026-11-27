#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""【仅在本机运行】从 WorkBuddy 登录态导出 refresh token，写入 GitHub 仓库 Secret。

作用：云端 Actions 没有本机登录态，也没有 AES 解封密钥（5.6.2+ 密钥只驻留
客户端进程内存）。因此 refresh token 必须在本机导出一次，写入仓库 Secret；
之后云端每次运行只拿 Secret 里的 refresh token 去官方刷新端点换 access token。

安全约定：
  * 本脚本只在你的本机运行，不要放进 Actions 工作流。
  * token 明文不写入仓库任何文件，不进入命令行参数，不打印到日志。
  * 默认通过 `gh secret set --body-file -` 从 stdin 传入 token；
    若本机没有 gh CLI，可用 --out 写到仓库外的 0600 文件再手工设置 Secret。
  * 需要 WorkBuddy 客户端已登录并正在运行（用于解封 AES 信封）。

用法：
  python scripts/export_token.py --repo owner/name
  python scripts/export_token.py --repo owner/name --secret WB_REFRESH_TOKEN
  python scripts/export_token.py --out /path/outside/repo/wb_rt.txt   # 无 gh 时
  python scripts/export_token.py --check                             # 只检查本机登录态
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import wb_auth  # noqa: E402  同目录，已验证的登录态读取辅助

DEFAULT_SECRET = "WB_REFRESH_TOKEN"


def load_refresh_token() -> tuple[str, str]:
    """返回 (refresh_token 明文, 登录态路径)。任何输出都不含 token。"""
    errors: list[str] = []
    for path in wb_auth._auth_candidates():
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                auth = json.load(f).get("auth", {})
            raw = auth.get("refreshToken")
            if raw is None:
                errors.append(f"{path}: 缺少 refreshToken 字段")
                continue
            wb_auth._check_expiry(auth)
            token = wb_auth.decrypt_access_token_field(raw)
            if not token or len(token) < 200:
                errors.append(f"{path}: refreshToken 内容异常")
                continue
            return token, path
        except Exception as exc:  # noqa: BLE001 - 逐候选降级，不打印 token
            errors.append(f"{path}: {type(exc).__name__}: {str(exc)[:120]}")
    raise RuntimeError(
        "无法从本机登录态导出 refresh token。请确认 WorkBuddy 客户端已登录且正在运行。\n"
        + "\n".join("  - " + e for e in errors)
    )


def mask(token: str) -> str:
    if len(token) <= 16:
        return "***"
    return token[:6] + "..." + token[-4:]


def write_to_gh_secret(token: str, secret: str, repo: str) -> int:
    """用 gh CLI 把 token 从 stdin 写进仓库 Secret，避免明文出现在参数/日志。"""
    cmd = ["gh", "secret", "set", secret, "--repo", repo, "--body-file", "-"]
    try:
        proc = subprocess.run(cmd, input=token, text=True, capture_output=True)
    except FileNotFoundError:
        print("未找到 gh CLI。请先安装 GitHub CLI 并 `gh auth login`，或改用 --out。", file=sys.stderr)
        return 2
    if proc.returncode != 0:
        # gh 的错误输出不会包含我们传进去的 token（token 走 stdin）
        print("gh secret set 失败：" + (proc.stderr or proc.stdout).strip()[:500], file=sys.stderr)
        return proc.returncode
    print(json.dumps({
        "status": "ok", "action": "secret_updated", "repo": repo,
        "secret": secret, "token_masked": mask(token),
        "note": "请在 Actions 页面确认 Secret 已更新；云端只需这一个 Secret 即可换取 access token。",
    }, ensure_ascii=False, indent=2))
    return 0


def write_to_file(token: str, out_path: str) -> int:
    """写到仓库之外的 0600 文件（仅当无法使用 gh CLI 时）。"""
    abs_path = os.path.abspath(out_path)
    repo_root = os.path.dirname(os.path.dirname(HERE))
    if abs_path.startswith(repo_root):
        print("拒绝把 token 写到仓库目录内，请在仓库外选择路径。", file=sys.stderr)
        return 2
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    try:
        os.chmod(abs_path, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:  # noqa: BLE001 - Windows 上 chmod 可能无效
        pass
    print(json.dumps({
        "status": "ok", "action": "token_exported_to_file", "path": abs_path,
        "token_masked": mask(token),
        "note": "请把文件内容手工填入仓库 Secret " + DEFAULT_SECRET + "，随后删除该文件。",
    }, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="本机导出 WorkBuddy refresh token 并写入 GitHub Secret")
    ap.add_argument("--repo", help="owner/repo，例如 zhangsan/buddy-actions")
    ap.add_argument("--secret", default=DEFAULT_SECRET, help="Secret 名称（默认 WB_REFRESH_TOKEN）")
    ap.add_argument("--out", help="把 token 写到仓库外的 0600 文件（无 gh CLI 时使用）")
    ap.add_argument("--check", action="store_true", help="只检查本机登录态是否可解封，不导出")
    args = ap.parse_args()

    try:
        token, path = load_refresh_token()
    except Exception as exc:  # noqa: BLE001
        print(str(exc), file=sys.stderr)
        return 1

    if args.check:
        print(json.dumps({
            "status": "ok", "auth_file": path,
            "token_masked": mask(token), "token_length": len(token),
            "note": "本机登录态可正常解封；不带 --check 运行即可写入 Secret。",
        }, ensure_ascii=False, indent=2))
        return 0

    if not args.repo and not args.out:
        print("请指定 --repo owner/name（写入 Secret）或 --out 路径（导出到仓库外文件）。", file=sys.stderr)
        return 2

    if args.out:
        return write_to_file(token, args.out)
    return write_to_gh_secret(token, args.secret, args.repo)


if __name__ == "__main__":
    raise SystemExit(main())

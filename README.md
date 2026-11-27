# Buddy 加油站 + ima 算力 · GitHub Actions 每日自动签到

每天在 GitHub 免费 schedule 定时任务上跑，**单次运行内完成 Buddy 签到 + 派猫**，
另有一个独立任务每天领 **ima 算力**。结果分别推送到飞书，一眼分清。纯云端 runner，不需要任何常开设备。

| 工作流 | 文件 | 北京时间 | 内容 |
|---|---|---|---|
| Buddy 加油站 | `.github/workflows/daily.yml` | 每天 06:00 | 签到 + 派猫 + 飞书卡片 |
| ima 算力 | `.github/workflows/ima.yml` | 每天 07:00 | 领每日算力 + 周日试领满签奖励 + 飞书卡片 |

两个任务独立运行、故障互不影响。

## 一、它怎么工作

```
GitHub Actions (每天 06:00 北京时间)
   │
   ├─ 1. 读 Secret WB_REFRESH_TOKEN → 官方刷新端点换 access token
   ├─ 2. 签到：POST checkin-activity-status 查状态 → 未签则 POST daily-checkin
   ├─ 3. 派猫：GET travel/status
   │        ├─ arrived → POST travel/claim 领取已到家积分 → 重新查状态
   │        └─ idle 且今日未派 → POST travel/depart 派新一趟（今日已派则跳过）
   ├─ 4. 飞书：签到结果与猫猫状态分两个区块展示
   └─ 5. 退出码：只看签到结果；猫猫失败不影响整体成功
```

## 二、真实接口（以本机 app.asar 代码 + 实测请求返回为准）

| 用途 | 方法 | 路径 | 基址 |
|---|---|---|---|
| 刷新 access token | POST | `/v2/plugin/auth/token/refresh` | `https://copilot.tencent.com` |
| 签到状态 | POST | `/v2/billing/meter/checkin-activity-status` | `https://www.codebuddy.cn/v2` |
| 每日签到 | POST | `/v2/billing/meter/daily-checkin` | `https://www.codebuddy.cn/v2` |
| 旅行状态 | GET | `/activity/growth/buddy/travel/status` | `https://www.workbuddy.cn` |
| 旅行地点配置 | GET | `/activity/growth/buddy/travel/config` | `https://www.workbuddy.cn` |
| 领取旅行积分 | POST | `/activity/growth/buddy/travel/claim` | `https://www.workbuddy.cn` |
| 派出猫猫 | POST | `/activity/growth/buddy/travel/depart` | `https://www.workbuddy.cn` |

> 注意：签到接口带 `/v2` 前缀；旅行接口**不带** `/v2`，且域名是 `www.workbuddy.cn`。
> 路径均为本机 WorkBuddy 5.11.3 的 `app.asar` 内实际代码与实测返回确认，
> 未采用网上流传的旧接口名。若官方改版，按返回码/字段调整即可。

**派猫前置条件**：账号需已在 WorkBuddy 成长计划中激活 Buddy（`/activity/growth/buddy/info`
返回的 `buddy` 不为 null）。未激活时 `travel/depart` 会返回 `no active buddy`，
本脚本会识别并跳过派遣，不报错、不影响签到结论。

## 三、一次性配置

### 1. 本机导出 refresh token（只需在有客户端的电脑做一次）

WorkBuddy 5.6.2+ 的 `accessToken`/`refreshToken` 是 AES-256-GCM 信封，
解封密钥只随客户端进程驻留内存，云端拿不到。因此在本机解封后写入仓库 Secret：

```bash
# 确认 WorkBuddy 已登录且正在运行
python scripts/export_token.py --check

# 方式 A：本机装了 gh CLI（推荐），token 从 stdin 传入，不进参数、不进日志
python scripts/export_token.py --repo <你的用户名>/<仓库名>

# 方式 B：没有 gh CLI，导出到仓库外的 0600 文件，手工填入 Secret 后删除文件
python scripts/export_token.py --out /tmp/wb_rt.txt
```

### 2. 在仓库 Settings → Secrets and variables → Actions 添加

| 名称 | 值 | 必填 |
|---|---|---|
| `WB_REFRESH_TOKEN` | 上一步导出的 refresh token 明文 | ✅ |
| `FEISHU_WEBHOOK` | 飞书群自定义机器人 Webhook 地址 | ✅ |
| `FEISHU_SECRET` | 飞书机器人加签密钥（若启用了签名校验） | 可选 |
| `WB_LOCATION_ID`（Variables） | 指定派遣地点 id；不填则随机 | 可选 |

### 3. 启用定时任务

把仓库推到 GitHub，Settings → Actions → General 确认 Actions 已启用。
工作流默认每天 UTC 22:00（北京时间 06:00）运行；也可在 Actions 页面手动 Run workflow 验证。

> refresh token 有较长有效期（约 30 天），到期后本机重新执行一次 `export_token.py` 即可。

## 四、安全设计

- **token 明文不入库、不进日志**：云端只从 Secret 读 refresh token；
  所有日志/证据经 `redact()` 脱敏，JWT 与长串统一替换为 `<redacted>`。
- **本机导出**：`export_token.py` 只在本机运行，token 从 stdin 交给 `gh`，
  不出现在命令行参数；`--out` 时强制写到仓库外并设 0600。
- **解释器动态获取**：工作流用 `command -v python3 || command -v python`，不硬编码版本号。
- **接口名以实测为准**：见上表。
- **猫猫失败隔离**：`do_travel()` 吞掉所有异常，签到成功即退出码 0。

## 五、飞书消息样式

飞书卡片分两个区块，一眼分清：

- **① 签到结果**：成功/失败、动作（新签/已签跳过）、说明、连签天数、本期累计积分
- **② 猫猫状态**：状态（空闲/旅行中/已到家）、派遣地点、旅行奖励、剩余时间、本次动作明细

## 六、验收证据

`evidence/` 目录包含一次成功运行的**脱敏后原始接口返回**：
- `evidence/run_20261127.json`：本机实测各接口的原始返回（已脱敏）

字段说明：`http_status` 是 HTTP 状态码，`raw_response_redacted` 是脱敏后的原始 JSON 返回体。

## 七、ima 每日算力签到

`.github/workflows/ima.yml` + `scripts/ima_daily.py`，每天北京时间 07:00 云端运行一次。

**机制**：ima 桌面端没有"点签到"动作——每天刷新登录态时"每日登录福利"就自动到账。
所以脚本做的是：用长期 `refresh_token` 换 2 小时有效的 access token → 查活动状态 →
未签则 `POST /cgi-bin/daily_login_activity/check_in` → 周日再试领一次满签奖励 → 推飞书。

**一次性配置**：

1. Secret `IMA_CREDENTIALS`：一个 JSON 字符串，含 `ima_uid`、`uid_type`、`ima_guid`、`refresh_token`
   四个字段（由微信扫码授权一次得到）。写法：
   ```bash
   # 本机临时目录运行扫码脚本，微信扫码一次
   python ima_recover.py --force          # 生成 login_qr.png，扫码后写 credentials.json
   # 把 credentials.json 的内容整体作为一个 Secret（不入库）
   ```
2. `FEISHU_WEBHOOK` / `FEISHU_SECRET` 与 Buddy 任务共用，无需重复配置。

**凭证失效**：ima 的 refresh_token 有效期由服务端控制。若某天签到失败并收到 GitHub 邮件告警，
本机重新跑一次 `ima_recover.py --force` 扫码，把新的 `credentials.json` 覆盖 `IMA_CREDENTIALS` Secret 即可。

**安全**：凭证只存 Secret，脚本从 `IMA_CREDENTIALS` 环境变量读入内存，不落盘、不进日志；
飞书卡片只显示签到进度与算力累计，无任何 token。

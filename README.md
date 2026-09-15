# Host-Ship Auto Renew

通过 GitHub Actions 检查 Host-Ship 免费服务器，在面板允许续期时提交确认，并发送 Telegram 图文通知。

## 续期与通知

- 以服务器页面中**可用的 `Renew` 按钮**判断续期窗口，确认弹窗中的 `Renew now` 只提交一次。
- 面板倒计时不是“距离可续期还有多久”。例如倒计时还有 4 天时，也可能允许续期至 14 天。
- 只有倒计时增加，或本次操作出现新的明确成功提示，才报告续期成功。旧提示、`Renew Limit Reached` 和无法确认的结果不会被当作成功。
- 手动运行会发送检查结果；定时运行在当前不可续期时保持安静，实际续期、失败或结果不确定时发送通知。
- 正常每次执行发送一条“截图 + 文字说明”的 TG 消息。截图或图片上传失败时改发文字；网络超时后的备用发送可能造成重复投递。
- 通知包含服务器编号、节点状态、出口 IP、检查时间、面板倒计时、运行编号、尝试次数和运行链接，便于区分手动重跑。
- 同一仓库的续期任务串行运行，避免同时操作同一服务器。
- 页面连接失败或临时 5xx 响应最多尝试三次；不会自动重发登录提交或续期确认。
- 不自动处理需要人工完成的验证码或安全验证。

## GitHub Secrets

在 **Settings → Secrets and variables → Actions** 配置：

| 名称 | 用途 |
| --- | --- |
| `SERVER_URL` | 服务器详情页，如 `https://panel.host-ship.com/server/xxxxxxxx` |
| `HOSTSHIP_LOGIN` | 登录账号或邮箱 |
| `HOSTSHIP_PASSWORD` | 登录密码，保留原始首尾空格 |
| `TG_BOT_TOKEN` | Telegram Bot Token |
| `TG_CHAT_ID` | 接收通知的聊天 ID |
| `NODE_LINK` | 可选的代理分享链接；留空时直连 |

不配置 TG 时仍可执行检查和续期。不要把真实账号、节点、服务器地址或 Token 提交进仓库。

## 运行计划

当前沿用工作流中的 `20 21 */3 * *`：按 UTC 每月 1、4、7……日的 21:20 触发，对应次日北京时间 **05:20**。大致每三天检查一次，跨月间隔会受月份长度影响。

修改计划时，同时更新 [.github/workflows/renew.yml](.github/workflows/renew.yml) 中的 `schedule` 和通知用的 `SCHEDULE_LABEL`。GitHub Actions 的实际启动时间可能延迟。

手动运行：**Actions → Host-Ship Auto Renew → Run workflow**。调试时可以关闭“发送 Telegram 图文通知”。重跑同一运行的任务属于另一次执行，会重新发送通知。

## 公开日志隐私

- 公开日志仅输出预先定义的执行状态，以及不含响应正文的 HTTP 状态码。
- 浏览器、代理脚本和其他依赖的原始输出不会直接进入公开日志。代理初始化只能向后续步骤传递启用标志和不含凭据的本地代理地址。
- 页面正文、服务器标识、出口 IP、完整 URL 和异常详情不会打印到 Actions 日志中。
- 截图在内存中生成，遮住输入框后只发送到 `TG_CHAT_ID` 指定的聊天；不上传 Actions 截图附件。
- TG 接收方仍能看到通知和页面截图中的服务器信息，请按需要设置接收聊天。
- 这些保护只作用于新运行。以前的公开日志或附件需要在 Actions 中自行清理。

## 代理

配置 `NODE_LINK` 后，工作流使用第三方初始化脚本：

`https://main.ssss.nyc.mn/setup_proxy.sh`

初始化失败会有限重试，仍失败则停止任务。日志过滤不会改变第三方脚本本身的运行权限。VMess/VLESS 是原模板已验证的类型，其他协议取决于转换脚本的支持情况。

## 本地验证

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m playwright install chromium
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python -m unittest discover -s tests -v
```

测试使用模拟接口和浏览器页面，不需要真实 Secrets。它们覆盖登录跳转、确认按钮范围、重复提交、续期成功判定、截图备用通知和公开日志隔离。

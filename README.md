# BOSS 直聘简历投递助手

这是一个以 CLI 为主的个人求职辅助工具：它通过 Chrome 的正常可见页面读取 BOSS 在线简历或本地简历，扫描岗位、调用 OpenAI 兼容 LLM 进行匹配与生成招呼语，并在你对当前批次明确授权后才发起沟通。

仓库地址：[kirayoushikake/Boss-Zhipin-Auto-Job-Application](https://github.com/kirayoushikake/Boss-Zhipin-Auto-Job-Application)。本 checkout 缺少完整的 `src-tauri/` 与前端构建源码，因此本版本只承诺 CLI；不声称桌面端可构建。

## 安全边界

- 默认 `dry-run`，不会发起 BOSS 沟通或发送简历。
- 每一次批次都需要在当前终端输入精确的 `YES`，以授权简历/JD 外发到配置的 LLM。
- `execute` 除第二次当前终端授权外，还必须对当前代码版本、账号、简历指纹和关键配置完成 A–F `LIVE_READY` 验收；批次与每日上限默认是 5 和 20。
- 只使用正常、可见的网页 UI；不读取 Cookie、不调用隐藏接口、不绕过验证码或平台限制。
- 仅当当前聊天对象匹配且页面出现唯一、独立、带稳定节点标识的新增本人消息时，才记作确认；输入框、历史文字、容器重复或无法确认都会记作 `unknown` 并安全停止，绝不自动重发。
- API Key、Chrome profile、SQLite 状态库、日志、向量缓存和简历均被 Git 忽略。不要把真实 Key 粘贴到命令行、日志或公开仓库；若曾泄露，请立即在服务商后台轮换。

## Windows 快速开始

在 PowerShell 中执行：

```powershell
cd "E:\a办公\简历\BOSS直聘简历投递助手"
Copy-Item .env.example .env
# 用文本编辑器填写 .env：LLM_API_KEY、LLM_BASE_URL=https://api.deepseek.com、LLM_MODEL=deepseek-chat
python -m uv sync --python 3.13 --no-group tauri
python -m uv run python -m boss_zhipin doctor
```

不要把真实 API Key 写到 `.env.example`、README 或 PowerShell 历史中。`doctor` 只报告“已配置（已隐藏）”，不会回显密钥。

### 命令

```powershell
# 检查当前 BOSS 在线简历，首次可能需要手动扫码；只读
python -m uv run python -m boss_zhipin inspect-resume --resume-source boss

# 本地回退：PDF 或 UTF-8 文本简历
python -m uv run python -m boss_zhipin inspect-resume --resume-source pdf --resume-path ".\resume\my_cover.pdf"

# 完整的可恢复验收向导 A–F；B 会显示脱敏摘要并要求确认，C 会记录要求/成功/失败/合格率
# 每个真实外部操作都会停下来等待确认，整个验收过程复用同一个可见 Chrome 页面
python -m uv run python -m boss_zhipin acceptance --preset game --city-code 101280600

# 真实页面的模拟批次：仍会要求授权本次 LLM 调用，但不会向 BOSS 写入
python -m uv run python -m boss_zhipin batch --resume-source boss --queries "游戏运营,游戏策划,游戏用户研究" --city-code 101280600 --batch-limit 5 --mode dry-run

# 仅生成并持久化待人工审核候选，不发消息
python -m uv run python -m boss_zhipin batch --resume-source boss --mode review

# 查看、批准或拒绝待审核草稿。pending_review 在批准前绝不会成为自动发送候选。
python -m uv run python -m boss_zhipin review list
python -m uv run python -m boss_zhipin review approve --job-key "boss-job:岗位稳定标识"

# 真实沟通：只有当前 A–F 为 LIVE_READY 时才可运行；仍会依次要求 LLM 授权和本批 BOSS 写入授权
python -m uv run python -m boss_zhipin batch --resume-source boss --batch-limit 3 --mode execute

# 查看脱敏历史（不显示完整招呼语）
python -m uv run python -m boss_zhipin history
```

旧的无参数入口现在只显示帮助，不会自动打开浏览器或发送消息。请使用上述明确的子命令。

### 搜索意向与浏览器会话

未指定搜索词时，工具从已确认简历的求职意向生成建议；不会强加游戏岗位。`--preset game` 是可选预设，包含“游戏运营、游戏策划、游戏用户研究”。`BOSS_PREFER_COMPANY_KEYWORDS`（或 `--prefer-company-keywords`）仅将命中二次元/游戏偏好的公司和岗位卡排在前面，不会排除其他公司。每个搜索词使用同一个正常可见的 Chrome 标签页依次导航，并在当前可见卡片不足时使用正常页面滚动加载；一个连续的 `batch` 或 `acceptance` 进程不会为每个岗位打开、关闭窗口。只有 CDP 已确认失效、或你在 GUI 中明确重置时才重启专用浏览器。

如果在线简历没有展开完整项目材料，可把可核验的项目事实保存在私有的 `resume/candidate_profile.md`（从 [模板](resume/profile_supplement.example.md) 复制）并在 `.env` 设置 `BOSS_PROFILE_SUPPLEMENT_PATH`。此文件会参与匹配、招呼语的事实校验和去重指纹，但被 Git 忽略，不会上传到仓库。

## 验收向导

`acceptance` 持久化 A–F 阶段，因此扫码、人工核对或发送授权中断后可从安全阶段继续：

| 阶段 | 操作 | 是否会写入 BOSS |
| --- | --- | --- |
| A | 环境、依赖、配置与 SQLite 检查 | 否 |
| B | 正常页面登录、读取在线简历、展示脱敏结构摘要并取得确认 | 否 |
| C | 逐项记录要求扫描数、成功数、失败原因和合格率的身份/JD 只读扫描 | 否 |
| D | 真实简历/JD 的 LLM Dry-run，核对分数、草稿和零平台写入 | 否 |
| E | 最多 1 个岗位的真实试投，核对目标身份和独立消息证据 | 是，须再次授权 |
| F | 最多 3 个岗位的小批量试投，核对预期/实际尝试、成功、失败、unknown 和额度 | 是，须独立授权 |

在没有你的登录、扫码和明确授权时，E/F 不会运行；验收记录同时绑定代码版本、稳定账号标识、简历指纹和关键匹配/模型策略。任一项变化都会让 `LIVE_READY` 失效。未实际验证的页面选择器不能视为已通过实站验收。

## 数据位置

- SQLite 状态与去重记录：`data/boss_zhipin.sqlite3`（可用 `BOSS_STATE_DB_PATH` 覆盖）
- 诊断与 LLM telemetry：`logs/`
- 本地向量/关键词缓存：`vectorstores/`
- 专用 Chrome 登录 profile：`chrome_profile/`（可用 `BOSS_CHROME_PROFILE` 覆盖）

这些目录只保存在本机。不要删除 `unknown` 状态来“重试”；先在 BOSS 正常聊天页面手动核对结果。

## 已知限制

- BOSS 页面会变化，在线简历读取、岗位身份识别、消息回显与简历分享选择器必须经当前账号的验收向导实际验证。
- 当前实现尚未在本机本轮连接真实 BOSS 页面验证在线简历、聊天独立消息节点和简历分享 DOM；它们在运行 A–F 前均属待验证。即使独立回显确认，也不等同于招聘方已阅读或回复。
- “发送简历/分享简历”控件并非所有账号、岗位或页面都提供；`best_effort` 会如实记录不可用/未知，`require_confirmed_share` 在未确认时会停止。
- 你应自行遵守 BOSS 直聘及所在地区的服务条款和使用限制。

## 测试

```powershell
python -m uv run pytest tests/ -v --tb=short
```

测试不会调用真实 BOSS，也不会调用付费 LLM。

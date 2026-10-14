# 维护约定

## Security 包依赖版本

用户约定（2026-09-08）：以后不要因为安全客户端修复或 Security 发布新版本，
就自动提高 Runtime 对 `bianbu-agent-securityd` 的最低依赖版本。
安全客户端修复在 Security 仓维护和发布；Runtime 的修复与发布不应自动附带
Security 最低依赖版本递增。确有新的硬性协议依赖时，先说明原因并取得用户确认。

## 提交消息语言

用户约定（2026-09-23）：本仓所有提交一律使用全英文，包含 commit subject、
正文、tag 消息和 changelog 条目，不要出现中文或任何非 ASCII 字符。
提交信息遵循约定式提交格式 `<type>(<scope>): <description>`，description 用英文祈使句。

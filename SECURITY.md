# Security Policy / 安全政策

## Reporting a vulnerability / 报告安全问题

Please do not disclose exploitable vulnerabilities in a public issue. Use GitHub's private vulnerability reporting feature for this repository. Include the affected version, reproduction steps, impact, and any suggested mitigation.

请勿在公开 Issue 中披露可利用的安全漏洞。请使用本仓库的 GitHub 私密漏洞报告功能，并提供受影响版本、复现步骤、影响范围和建议的缓解方案。

## Deployment guidance / 部署建议

- Replace every example password and token in `.env`.
- Keep `ALLOW_PRIVATE_TARGETS=false` unless private-network scanning is explicitly required and trusted.
- Place the portal behind HTTPS and an authentication proxy before exposing it publicly.
- Restrict scanner containers' outbound network access where possible.
- Back up PostgreSQL and rotate credentials regularly.
- Treat generated reports as potentially sensitive because they may contain URLs and HTML snippets.

- 修改 `.env` 中的所有示例密码和令牌。
- 除非明确需要并信任内网站点扫描，否则保持 `ALLOW_PRIVATE_TARGETS=false`。
- 对公网开放前，请在 Portal 前配置 HTTPS 和身份认证代理。
- 尽可能限制扫描容器的外部网络访问。
- 定期备份 PostgreSQL 并轮换凭据。
- 生成的报告可能包含 URL 和 HTML 片段，应按敏感数据处理。

# Contributing / 贡献指南

Thank you for helping improve OpenAudit Hub. Issues and pull requests are welcome for accessibility rules, scanner integrations, documentation, tests, and user experience.

感谢你参与改进 OpenAudit Hub。我们欢迎与无障碍规则、扫描器集成、文档、测试和用户体验相关的 Issue 与 Pull Request。

## Development setup / 开发环境

```powershell
Copy-Item .env.example .env
docker compose up -d --build postgres redis portal scan-worker scan-scheduler
```

Run the local checks before opening a pull request:

提交 Pull Request 前请运行：

```powershell
python -m compileall -q services tests scripts/crawl-sitemaps.py
python -m unittest discover -s tests -v
docker compose config --quiet
```

## Pull requests / Pull Request 要求

- Keep each change focused and explain the user-facing outcome.
- Add or update tests when behaviour changes.
- Do not commit `.env`, tokens, databases, generated reports, or customer website data.
- Preserve website isolation: every report, issue, and recommendation must be scoped to the selected website.
- Include screenshots for visible interface changes.

- 每次改动应保持聚焦，并说明对用户产生的影响。
- 行为发生变化时，请增加或更新测试。
- 不要提交 `.env`、令牌、数据库、生成报告或客户网站数据。
- 必须保持网站数据隔离：报告、问题和建议都应属于当前选中的网站。
- 界面改动请提供截图。

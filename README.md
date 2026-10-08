# GitHub ↔ Gitee Bridge

[![Tests](https://github.com/Tianchen-ai/github-gitee-bridge/actions/workflows/test.yml/badge.svg)](https://github.com/Tianchen-ai/github-gitee-bridge/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](requirements-bridge.txt)

**让 GitHub 和 Gitee 成为同一项目的两个入口。**

GitHub ↔ Gitee Bridge 将 GitHub 的代码、Issue 和 Pull Request 同步到 Gitee，并可将 Gitee 的讨论回传到 GitHub。适合以 GitHub 为主仓库、同时希望为国内用户提供代码镜像和协作入口的项目。

一个服务、一个 SQLite 数据卷，即可通过 Docker 部署。支持定时同步和 Webhook，不依赖 Redis 或外部数据库；在没有公网入口的环境中也能通过定时核对运行。

## 功能

| 内容 | GitHub → Gitee | Gitee → GitHub |
| --- | --- | --- |
| 仓库、commits、branches、tags | 创建目标仓库，同步提交和引用 | — |
| Issues | 创建、编辑、关闭、重开 | — |
| Issue comments | 新增、编辑、来源作者和链接 | 可选，限已映射 Issue |
| Pull Requests | 创建、标题/正文、源分支、head SHA、状态说明 | — |
| PR discussions | 普通评论；代码行评论转为带上下文的普通评论 | 可选，限已映射 PR |
| Labels | 名称映射、颜色、Issue/PR 标签分配 | — |
| Milestones | 有截止日期的里程碑及其分配 | — |

两边的编号无需相同。Bridge 保存稳定映射，将它们视为同一个逻辑对象：

```text
GitHub Issue #31  ↔  Gitee Issue Ixxxxx
GitHub PR    #52  ↔  Gitee PR    #18
```

评论以同步账号发布，并标注原作者、来源平台和原始链接。重复 Webhook 不会反复创建对象；任务失败后重试，定时核对可补偿遗漏事件。

## 快速开始

需要 Docker Engine、Docker Compose，以及具有相应仓库权限的 GitHub/Gitee token。

```bash
git clone https://github.com/Tianchen-ai/github-gitee-bridge.git
cd github-gitee-bridge

cp bridge.example.toml bridge.toml
cp .env.bridge.example .env.bridge
chmod 600 .env.bridge
```

编辑 `bridge.toml`，填写仓库映射。两边的仓库名可以不同：

```toml
direction = "github2gitee"
interval = 300

[sync]
git = true
issues = true
issue_comments = true
pull_requests = true
pr_comments = true
reverse_comments = false

[[repositories]]
github = "github-owner/project"
gitee = "gitee-owner/project"
gitee_account_type = "user" # 组织仓库使用 "org"
```

在 `.env.bridge` 中填写 `GITHUB_TOKEN`、`GITEE_TOKEN`，并为 `GITHUB_WEBHOOK_SECRET` 设置长随机字符串。可以用 `openssl rand -hex 32` 生成。启用 Gitee Webhook 时，再设置独立的 `GITEE_WEBHOOK_SECRET`。

```bash
docker compose build
docker compose run --rm bridge check  # 检查配置
docker compose run --rm bridge once   # 完成首次同步
docker compose up -d                  # 定时同步与 Webhook 服务
```

GitHub token 默认需要 Contents、Issues 和 Pull requests 的读取权限；开启评论回传后，另需 Issues/Pull requests 写入权限。Gitee token 需要目标仓库推送、协作内容读写及用户信息读取权限。个人目标仓库的 owner 必须与 Gitee token 所属用户一致。详见[权限与部署配置](docs/operations.md)。

**GitHub 是主源：同名分支和标签会覆盖 Gitee 上的对应引用；Gitee 独有引用保留。** 私有源仓库默认不允许同步到公开目标仓库。

## 接入 Webhook

定时同步开箱可用，Webhook 用于更快地触发更新：

| 平台 | 地址 | 验证方式 |
| --- | --- | --- |
| GitHub | `/webhooks/github` | `X-Hub-Signature-256`，HMAC-SHA256 |
| Gitee | `/webhooks/gitee` | 密码模式，需开启 `reverse_comments` |

Compose 默认只绑定 `127.0.0.1:8080`。需要接收平台推送时，在前面部署 HTTPS 反向代理，填写完整公网地址。没有公网访问条件时，无需配置 Webhook。

Webhook 只触发任务，后台始终读取平台当前状态，避免旧事件覆盖新状态。[查看事件订阅与配置说明 →](docs/operations.md)

## 状态与数据

```bash
docker compose logs -f
docker compose exec bridge python -m bridge --config /app/bridge.toml status
docker compose exec bridge python -m bridge --config /app/bridge.toml mappings
```

SQLite 保存对象映射、投递记录和任务进度。请保留 `bridge-state` 数据卷，并让同一组仓库只由一个实例处理。`/healthz` 检查服务存活；同步结果、重试和最后成功时间通过 `status` 查看。

远端创建成功但响应丢失时，Bridge 会通过来源标记找回对象。如果无法确定是否创建成功，会保留待核查状态，避免盲目重试制造副本。[备份、监控和故障恢复 →](docs/operations.md)

## 同步语义与边界

- **合并只在 GitHub 执行。** Gitee 正文展示主源状态；保留平台自动识别的 merged，否则关闭 PR 并注明已合并，不额外生成合并提交。
- **历史 PR 保留讨论与来源。** 首次导入时已经关闭或合并的 PR 使用 Issue 归档，避免依赖已删除的源分支。归档后重开仍保留 Issue 表示。
- **代码与状态单向，评论可双向。** 不支持双向 Git、Gitee 新建 Issue/PR 回传或跨平台状态合并。
- **平台特性保留在原平台。** Checks、CI、review approval、Projects 和 Discussions 通过 GitHub 来源链接访问，不复制原生审批和 diff thread。
- **不传播删除。** Gitee 独有分支/标签、评论和对象不会因为源端删除而自动删除。标签名称不符合 Gitee 规则时使用稳定映射。
- **差异有明确表示。** PR base 变更通过正文说明；无截止日期的里程碑保留来源信息，暂不保证清除目标已有里程碑分配。Git LFS 对象传输不在同步范围内。

PR head 通过 GitHub 的 `refs/pull/<number>/head` 获取，不依赖 fork 在 Gitee 上存在。跨账号 fork 权限、保护分支及不同合并策略需要按实际仓库配置确认。[完整行为说明 →](docs/operations.md)

## 其他运行方式

Python 3.10+ 与 Git：

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-bridge.txt
# 将 token 和 Webhook secret 设置为环境变量
.venv/bin/python -m bridge --config bridge.toml once
.venv/bin/python -m bridge --config bridge.toml serve
```

GitHub Actions 可定时运行 `once`，参见[自托管 runner 示例](examples/bridge-actions.yml)。必须使用持久化状态目录；不建议仅靠临时 runner 的缓存保存映射。

## 参与贡献

欢迎提交 bug、改进文档或扩展平台适配。请先阅读 [贡献指南](CONTRIBUTING.md) 与[架构说明](docs/bridge-architecture.md)。提交问题时请移除 token、Webhook secret 和私有仓库内容。

## 开源基础与许可证

本项目基于 [NEVSTOP-LAB/GitHub-Gitee-Sync](https://github.com/NEVSTOP-LAB/GitHub-Gitee-Sync)，复用 Git 镜像和认证实现，并增加持久化协作同步层。对象映射设计参考了 [OpenSiFli/gitee2github-issue](https://github.com/OpenSiFli/gitee2github-issue)，Git 同步边界参考了 [Yikun/hub-mirror-action](https://github.com/Yikun/hub-mirror-action)。

采用 [MIT License](LICENSE)，保留上游版权。原 `sync.py`、`action.yml` 和 `Dockerfile` 作为上游兼容入口保留，见 [legacy 文档](README.upstream.md)；**本项目的 Bridge 使用 `python -m bridge`、`Dockerfile.bridge` 和 Compose**。不要让两个入口同时同步相同协作对象。

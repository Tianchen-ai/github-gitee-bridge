# 部署与运维

基于 [NEVSTOP-LAB/GitHub-Gitee-Sync](https://github.com/NEVSTOP-LAB/GitHub-Gitee-Sync) 的扩展：保留 Git 同步实现，新增持久化协作对象映射、Webhook 和定时核对。GitHub 为代码和对象状态主源，Gitee 为镜像及讨论入口。MIT 许可证，保留上游版权。

本页说明配置、权限、Webhook、持久化和故障恢复。首次使用请从 [README](../README.md) 的快速开始进入。

## 支持范围

| 内容 | 当前行为 |
| --- | --- |
| 仓库 | 自动创建 Gitee 个人/组织仓库；两端名称和账号类型可不同 |
| commits / branches / tags | 复用上游 `mirror_sync`；同名引用由 GitHub 强制覆盖，保留 Gitee 独有引用，不传播删除 |
| Issues | 创建、标题/正文更新、关闭/重开；映射 GitHub ID 与 Gitee `Ixxxxx` |
| Issue comments | 新增、编辑、原作者/来源链接；不传播删除 |
| 开放 PR | 原生 Gitee PR；`bridge/pr/<number>/head` 专用分支，支持 fork PR，校验 head SHA |
| PR comments | 普通讨论同步；代码行评论转换为带文件、行号、commit 和回复上下文的普通评论 |
| PR state | open/closed 对应；GitHub merged 在正文明确标注，保留 Gitee 自动识别的 merged，否则关闭 PR；不调用 Gitee merge |
| 历史 closed/merged PR | 首次同步时用明确标注的 Issue 归档状态、来源和讨论，不伪造 diff |
| Labels | 名称/颜色及 Issue/PR 标签分配；不合法的 Gitee 名称采用稳定缩写加哈希并保存映射，正文保留原名；不传播仓库级标签删除 |
| Milestones | 有截止日期的里程碑和分配；无截止日期只保留来源说明，不编造日期 |
| Gitee → GitHub | 可选：已映射 Issue/PR 的用户评论新增、编辑 |
| CI / Checks / Review approval / Projects / Discussions | 留在 GitHub，通过来源链接访问 |
| Releases / Wiki | 上游 legacy CLI 保留；未纳入新 bridge 的持久化保证 |

PR 的逻辑状态以正文 `Canonical PR state` 为准。真实 merge-commit 测试中，Gitee 在收到镜像提交后自动识别为 merged；这不代表 squash/rebase 等情况也必然如此，原生界面仍可能显示 closed。历史 PR 归档后重开仍保留 Issue 表示。GitHub 修改 PR base 时，Gitee API 不支持 retarget，正文提示使用 GitHub 查看当前 diff。原文 `#123` 引用附加 GitHub 链接，不假设两边编号相同，不模拟原生自动关闭关系。

## Docker 部署（推荐）

```bash
cp bridge.example.toml bridge.toml
cp .env.bridge.example .env.bridge
# 编辑仓库映射、token 和 webhook secret
chmod 600 .env.bridge
docker compose build
docker compose run --rm bridge check
docker compose run --rm bridge once
docker compose up -d
docker compose logs -f
```

配置见 [bridge.example.toml](../bridge.example.toml)，支持多个 `[[repositories]]`。凭据只从环境变量读取：

- `GITHUB_TOKEN`：专用机器人账号，源仓库 Contents/Issues/Pull requests 读取权限；开启 `reverse_comments` 另需 Issues/Pull requests 写入权限。私有仓库需授权。
- `GITEE_TOKEN`：专用机器人账号的仓库/Issue/PR 读写和 `user_info` 权限；需要目标创建/推送权限。个人目标 owner 要与 token 用户一致；组织目标需有组织权限。
- `GITHUB_WEBHOOK_SECRET`：`serve` 必填，使用长随机值。
- `GITEE_WEBHOOK_SECRET`：反向评论 webhook 可选，仅支持 Gitee **密码模式**。没有 webhook 时定时核对仍可回传评论。
- `BRIDGE_STATE_DIR`：覆盖状态目录，Docker 默认 `/state`。

推荐使用专用机器人账号。防回环结合对象映射、来源标记和写入账号判定；token 所属账号手动发布的普通评论也能同步。同步内容显示原作者、平台和原始链接，不伪造身份。

Compose 只监听 `127.0.0.1:8080`，请在前面配置 HTTPS 反向代理：

- GitHub：`https://your-host/webhooks/github`，JSON，同一 secret；订阅 push、create/delete、issues、issue_comment、pull_request、pull_request_review_comment、label、milestone、repository。
- Gitee：`https://your-host/webhooks/gitee`，密码模式；订阅评论/Issue/PR，并开启 `reverse_comments=true`。

Webhook 验证后持久化任务并返回 202，后台读取最新 API 状态。未配置的仓库被拒绝，不保存原始 payload。没有公网入口也可以只依靠默认每 300 秒核对。

## 持久化与故障恢复

单个 Python 服务 + SQLite，无 Redis/外部数据库。必须保留 `bridge-state` volume，不要 `docker compose down -v`。同一状态目录只允许一个 worker；不要在不同 volume 上运行同一对仓库的实例，不要使用锁语义不可靠的网络文件系统。

```bash
docker compose exec bridge python -m bridge --config /app/bridge.toml status
docker compose exec bridge python -m bridge --config /app/bridge.toml mappings
```

`status` 显示重试、最后成功时间、错误和 pending intent。`/healthz` 只代表接收器与 worker 存活，不代表同步成功；监控还应检查 `error`、`pending` 和最后成功时间。失败退避重试，其他仓库继续；`once` 任一失败返回非零。

创建前提交 intent，创建后保存映射。远端正文有稳定来源标记，恢复时校验机器人作者。POST 成功但响应丢失时，下次列举找回对象；仍找不到时保持 uncertain，不盲目重发。平台缺少通用幂等键，不能承诺无条件 exactly-once。

uncertain 时先等待核对并检查远端；确认**确实未创建**后停止服务并解除 intent：

```bash
docker compose stop bridge
docker compose run --rm bridge resolve \
  --repository 'gh-owner/repo=>gt-owner/repo' \
  --kind issue --source 123456 --confirm-absent
docker compose up -d
```

参数使用 `status` 的原值；source 是对象 ID，通常不是界面编号。错误确认可能制造重复。已创建时不要解除，保留机器人作者和标记即可自动恢复。已映射对象在远端被删除时默认报错，避免重复重建。备份用 SQLite backup API，或停服务后备份整个 volume；不要只复制运行中的主文件、遗漏 WAL。

## 冲突策略

GitHub 代码同名引用、Issue/PR 标题正文状态和导入评论覆盖目标端修改。Gitee 新评论可回传，其回传副本由 Gitee 原评论控制。Gitee 独有分支保留，同名分支会被覆盖；代码修改请在 GitHub 提 PR，不要在 Gitee 合并镜像 PR。源仓库不得使用保留的 `bridge/pr/` 分支前缀。

## Python 与 GitHub Actions

Python 3.10+、Git：

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-bridge.txt
# 将 token 安全导出到当前环境
.venv/bin/python -m bridge --config bridge.toml check
.venv/bin/python -m bridge --config bridge.toml once
.venv/bin/python -m bridge --config bridge.toml serve
```

Actions 定时执行 `once`，见[自托管 runner 示例](../examples/bridge-actions.yml)。必须使用固定持久化目录并串行运行；临时 hosted runner 的 cache/artifact 不保证恢复 SQLite，不推荐作为唯一状态存储。不要在不可信 PR workflow 运行带 token 的同步。

已有 Git mirror 时可设 `sync.git=false`。开放 PR 仍要求外部工具提供 `bridge/pr/<number>/head` 且 SHA 与 GitHub 一致；普通 heads/tags mirror 不会自动生成 PR 分支。

## 验证和边界

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

真实测试仓库验收顺序：推送 main/feature/annotated tag → Issue 新建/编辑/关闭/重开 → 评论新增/编辑 → 同仓库及 fork PR → head 更新/讨论 → GitHub 合并 → 重投 Webhook → 重启服务 → 比较引用 SHA、对象数、状态和映射。开启回传后验证 Gitee 评论不回环。本仓库测试不会操作线上账号。

不复制原始身份/时间戳、删除、审核批准、原生 diff thread、自动关闭 Issue 关系。未实现双向 Git、双向 Issue 创建、双向状态。Gitee 标签要求 2–20 字符且不接受空格，名称映射可用 mappings 查询。里程碑移除或改为无截止日期只更新来源说明，暂不保证清除 Gitee 既有分配。每轮全量 API 分页和临时完整 Git 镜像，可能需调大 interval；未实现增量游标、LFS 对象传输或大规模吞吐优化。

详见[架构说明](bridge-architecture.md)。原 `sync.py`、`action.yml`、`Dockerfile` 和[旧说明](../README.upstream.md)保留兼容上游，旧 Issue 同步不具备新 bridge 的保证；不要让两个入口同时处理相同协作对象。

# Bridge 架构依据

## 实际阅读的源码

| 项目 | 固定 commit | 结论 |
| --- | --- | --- |
| [NEVSTOP-LAB/GitHub-Gitee-Sync](https://github.com/NEVSTOP-LAB/GitHub-Gitee-Sync) | `863334e675d0e1ff2bc41d0ae5c4bda51b12f6f2` | 阅读 `lib/sync_repo.py`、`lib/utils.py`、两端 API、tests；MIT，作为本项目 fork 基础 |
| [OpenSiFli/gitee2github-issue](https://github.com/OpenSiFli/gitee2github-issue) | `836b381538bda2386817d32503e8e05ad591d5ce` | 阅读 `src/services/sync-service.ts`、两端 service、`migrations/schema.sql`；吸收映射/来源思想，不复制代码；checkout 未找到许可证 |
| [Yikun/hub-mirror-action](https://github.com/Yikun/hub-mirror-action) | `ba51c01b28a6c9f95a25d4f1bcf6af2a147c0e18` | 阅读 `hub-mirror/mirror.py`；成熟独立 Git 工具，有 GitPython/refspec/prune/LFS；没有引入第二套服务 |

NEVSTOP 的 `sync_issues` 只列 open Issue，通过 body marker 跳过已导入对象；评论仅在创建时导入，缺少持续编辑/状态、PR、持久化映射和创建故障窗口处理。`paginated_get` 出错返回部分列表，`api_request` 重试 POST，不宜直接承载严格协作幂等。

保留完整上游仓库及历史、MIT、Git `mirror_sync`、`make_git_env`、认证 header、legacy CLI/Action 和测试，新增 `bridge/`。没有重写已有 Git 算法，只增加 PR ref fetch 和 SHA 检查。新的 HTTP adapter 对分页失败报错，POST 不自动重试。上游 create 将所有 422 当已存在，新入口改为实际 GET 确认目标。

OpenSiFli 的分表映射可借鉴，但 Gitee event ID 使用 hook_id + Date.now()，重投无法稳定去重；remote create 和 DB insert 存在故障窗口，文本防回环也不够。本实现只参考其模型。

## 数据流与故障边界

```
authenticated webhook → SQLite deliveries + repository jobs
periodic reconciliation ────────────────────────────────┘
                           ↓ one worker + filesystem lock
repository → upstream Git mirror → labels/milestones/Issue/PR/comments
                           ↓ SQLite mapping + create intent
```

Webhook 仅触发最新状态核对，乱序事件不回滚状态。任务 generation 在处理期间增长时不会被本次 done 覆盖。投递去重保留 30 天，之后仍有对象级保护。SQLite WAL + FULL synchronous，一个 volume 一个 worker；Git/API/SQLite 不组成全局事务。

主键 `(repository pair, kind, source object ID)`；目标编号用字符串，不假设 Gitee Issue 是整数。PR 保存 pulls/issues 表示，评论路由随之变化；反向评论单独 kind。Milestone 使用 repo 内编号。

创建协议：完整列举 → DB/机器人 marker 恢复 → 提交 pending → 单次 POST → 提交映射。没有证明 POST 未生效时阻止再次创建。仓库 path 和 label name 有平台唯一性，可自然恢复。不承诺无条件 exactly-once，不用当前时间生成幂等键。

## 官方 API 核验

实际读取 Gitee [Swagger](https://gitee.com/api/v5/swagger) 的 [doc_json](https://gitee.com/api/v5/doc_json)，版本 `5.4.93`：

- Issue 创建 `POST /repos/{owner}/issues`，body 含 repo；更新 `PATCH /repos/{owner}/issues/{number}`。列举/评论路径包含 owner/repo。
- PR 创建支持 head/base/title/body；更新只允许 open/closed，不能直接设 merged，也不支持 base retarget。merge endpoint 会执行代码合并，不用于复制状态。
- PR 评论支持普通/行评论；本版转换为来源普通评论，不复制审核结论。
- Milestone 必须有 due_on，不编造日期。
- GitHub [PR API](https://docs.github.com/en/rest/pulls/pulls) 的普通讨论使用 Issue comments，代码行讨论使用 review comments；[Webhook](https://docs.github.com/en/webhooks/webhook-events-and-payloads) 只触发核对。

这属于文档核验与模拟契约测试，不代表真实 Gitee 写入验证。保护分支、账号配额、组织权限及 PR 差异限制需要实际测试仓库确认。

## 双向范围

默认 GitHub canonical；可选反向路径只有已映射对象的评论。未实现 Gitee Issue/PR 创建回传、反向合并、CRDT、复杂身份映射。新建 Gitee Issue 回传应沿用来源平台 + object ID、intent/recovery，并单独定义状态冲突规则。Releases/wiki 仅保留 legacy，不混算新入口可靠性。

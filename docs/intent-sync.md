# 将 Intent 同步到东山社区

此配置将 [Tianchen-ai/Intent](https://github.com/Tianchen-ai/Intent) 同步到
[东山社区 / Intent](https://gitee.com/dongshan-community/Intent)。GitHub 为主源，目标是公开仓库。

## 配置

在 Bridge 项目根目录创建凭据文件：

```bash
cp .env.intent.example .env.intent
chmod 600 .env.intent
```

填写 `GITHUB_TOKEN` 和 `GITEE_TOKEN`。Gitee token 用户必须有东山社区目标仓库的推送与协作内容写入权限。
文件不会被 Git 跟踪，脚本不会将凭据放进命令行参数，也不会将该文件作为 shell 脚本执行。

仓库映射与功能开关位于 [examples/intent.toml](../examples/intent.toml)。
映射、去重和任务状态保存到 `state/intent/`；请保留并备份此目录。

## 手动同步

```bash
./sync-intent.sh
```

默认完成一次同步后退出，任意同步失败时返回非零。脚本会在缺少虚拟环境/依赖时创建 `.venv` 并安装依赖。
需要 Python 3.10+ 和 Git。可以从任意工作目录用脚本绝对路径运行。

```bash
./sync-intent.sh check     # 仅校验配置
./sync-intent.sh status    # 查看任务结果和错误
./sync-intent.sh mappings  # 查看跨平台对象映射
```

同步涵盖提交、分支、tag、Issue、PR、评论、标签和有截止日期的里程碑。
同名 Git 引用以 GitHub 为准；Gitee 独有引用保留。默认不回传 Gitee 评论。
PR 合并状态、历史归档及其他平台差异遵循 [Bridge 的同步语义](../README.md#同步语义与边界)。

## 定时运行

使用 `crontab -e` 添加如下示例，每 5 分钟执行一次，将路径替换为实际 Bridge 项目路径：

```cron
*/5 * * * * /path/to/github-gitee-bridge/sync-intent.sh >> /path/to/github-gitee-bridge/state/intent-sync.log 2>&1
```

添加定时任务前，先手动成功运行一次。建议对日志做轮转。
同一状态目录只允许一个同步进程；任务重叠时新进程会报错退出，不会并行创建对象。

如果需要常驻服务和 Webhook，配置两端 Webhook secret 后运行：

```bash
./sync-intent.sh serve --host 127.0.0.1 --port 8080
```

服务会按 `interval` 周期核对，不依赖公网 Webhook。生产常驻部署可参考 [Docker 运维说明](operations.md)。
同一仓库不要同时使用 cron、常驻服务和另一份独立数据库运行同步。

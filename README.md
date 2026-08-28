# Offwork V0.2：终端个人 Agent 工作台

Offwork 把长期任务、Agent Session、项目现场、可检索历史和可验证恢复包放在同一个零安装 CLI 中。它以 `offwork status` 为入口，帮助你回答三件事：现在最该做什么、相关 Agent 会话在哪里、换一个没有当前上下文的 Agent 能否安全继续。

V0.2 只依赖 Python 标准库，默认数据留在本机。它不会替你 commit、stash、push，也不会自动执行历史命令或所谓的“下一步”。

## 直接运行

```bash
cd /Users/eddie/Documents/WAIC/offwork-capsule
./bin/offwork --help
```

无需安装依赖。需要全局命令时，也可以执行 `python3 -m pip install -e .`。项目另带 Claude Code 技能 `.claude/skills/offwork/SKILL.md` 和 Codex 技能 `.agents/skills/offwork/SKILL.md`。

## 最小工作流

先创建一个带本地验收条件的长期任务：

```bash
./bin/offwork task add "完成离线恢复" \
  --goal "新 Agent 仅凭 Capsule 可以安全开始第一步" \
  --auto-complete \
  --accept-cmd "python3 -m pytest -q" \
  --project . \
  --json

./bin/offwork status --project .
```

`task add --json` 会返回 `task_id`。下面假设它已保存为 `TASK_ID`。如有前置任务，可以建立同项目依赖；只有前置任务为 `complete` 时，下游任务才会解除阻塞。

```bash
./bin/offwork task dependency add "$TASK_ID" "$DEPENDENCY_ID" --project .
./bin/offwork task start "$TASK_ID" --project .
./bin/offwork task list --actionable --project .
./bin/offwork task show "$TASK_ID" --project .
```

将现有 Agent 会话显式挂到任务上：

```bash
./bin/offwork session attach \
  --task "$TASK_ID" \
  --tool codex \
  --native-id "$CODEX_SESSION_ID" \
  --tmux "/tmp/offwork.sock:agent-main" \
  --project .

./bin/offwork session list --task "$TASK_ID" --project .
./bin/offwork session primary "$MANAGED_SESSION_ID" --project .
```

`session enter` 只进入已存在的 tmux 会话；`session reopen` 是用户显式触发的恢复动作，仅在 tmux 不存在时通过 Codex 或 Claude adapter 重新打开原生 Session。Offwork 不猜测“最近会话”，也不自动启动、停止或批量控制 Agent。

```bash
./bin/offwork session enter "$MANAGED_SESSION_ID" --project .
./bin/offwork session reopen "$MANAGED_SESSION_ID" --project .
```

准备 `context.json`，记录本次工作的项目现场：

```json
{
  "goal": "新 Agent 仅凭 Capsule 可以安全开始第一步",
  "summary": "恢复链路已实现，正在验证兼容性。",
  "decisions": ["保留 Python 标准库和 CLI-first"],
  "failed_attempts": ["自动猜测最近会话无法可靠绑定任务"],
  "next_step": "运行完整测试并检查恢复输出",
  "next_command": "python3 -m pytest -q",
  "open_loops": []
}
```

生成当前任务的 Capsule，并在下一次工作时恢复：

```bash
./bin/offwork capture \
  --task "$TASK_ID" \
  --context context.json \
  --project .

./bin/offwork resume --task "$TASK_ID" --recall auto --project .
```

`capture` 会保存限定在 `--project` 边界内的 Git 和文件现场，进行本地完整性检查，然后逻辑上休眠当前 primary Session，但不会杀死外部进程。`resume --recall auto` 只召回同项目、同 Task、显式关联且数量受限的记忆和历史摘录；用 `--recall none` 可完全关闭召回，也可以用 `--capsule <capsule-id>` 指定恢复包。

## 工作台命令

`status` 可查看当前推荐动作、actionable、blocked、waiting 任务、Session 和最近 Capsule；`--all` 读取全局轻量注册表，汇总已登记项目。

```bash
./bin/offwork status --project .
./bin/offwork status --all

./bin/offwork task list --blocked --project .
./bin/offwork task complete "$TASK_ID" --confirm --project .
./bin/offwork task archive "$TASK_ID" --project .
./bin/offwork task unarchive "$TASK_ID" --project .
./bin/offwork task dependency remove "$TASK_ID" "$DEPENDENCY_ID" --project .
```

归档是软归档：任务、Session 和 Capsule 历史仍然保留。手动或自动标记 `complete` 只表示配置的本地条件已通过，不代表客户验收、生产上线或任何外部事项已经完成。

## 历史搜索与持久记忆

Codex 和 Claude 历史需要逐项目显式启用。索引仅保留可见的 user/assistant 文本，并按项目和来源隔离。

```bash
./bin/offwork source enable codex --project .
./bin/offwork source enable claude --project .
./bin/offwork index --project .

./bin/offwork search "恢复测试" --task "$TASK_ID" --source codex --project .
```

FTS5 trigram 支持中文搜索，1–2 字查询会在已限定的项目和任务范围内安全回退。搜索结果会显示来源、时间、角色、Session 和证据位置；历史内容只说明过去说过什么，不能证明项目当前状态。

只有用户显式保存的内容才会成为持久记忆：

```bash
./bin/offwork memory add "恢复前先运行完整测试" --task "$TASK_ID" --project .
./bin/offwork memory list --task "$TASK_ID" --project .
./bin/offwork memory forget "$MEMORY_ID" --project .
```

## 自动完成与验证边界

启用 `--auto-complete` 后，Offwork 仍需同时满足以下条件才会将任务设为 `complete` 并软归档：至少配置一条验收命令；全部命令退出码为 0；没有仍需处理、park 或 delegate 的 open loop；Capsule 本地验证通过；如任务使用 `--require-fresh-verifier`，fresh verifier 也通过；检查期间任务 revision 未变化。

验收命令通过 `shlex.split` 拆成 argv，以项目根为固定 cwd 调用 `subprocess`，不会使用 `shell=True`。因此管道、重定向和 `&&` 不会被当作 shell 语法执行。

需要一次全新的 Claude CLI 会话验证恢复质量时：

```bash
./bin/offwork capture \
  --task "$TASK_ID" \
  --context context.json \
  --verifier claude \
  --project .
```

此选项会把本次选定的 Capsule 内容发送给当前 Claude CLI 配置的模型服务，并产生相应模型用量；敏感项目应先确认数据使用边界。外部 verifier 不可用与恢复验证失败会分别报告。本地验收失败时，已通过完整性检查的 Capsule 仍可作为恢复点，任务保持在 `review`，不会自动归档。

## 存储与兼容性

Offwork 使用分层本地存储：

- `$XDG_DATA_HOME/offwork/registry.sqlite3`：跨项目轻量索引，可重建。
- `<project>/.offwork/project.json`：随机项目身份。
- `<project>/.offwork/state.sqlite3`：Task、依赖、Session、记忆和来源索引的项目私有真值。
- `<project>/.offwork/capsules/<capsule-id>/`：不可变 `capsule.json`、`capsule.md`、`restore-test.json` 和完整性 manifest。

`.offwork` 目录使用 `0700`，数据库、锁文件和 Capsule 文件使用 `0600`。SQLite 启用 WAL、外键、busy timeout 和事务迁移。`--project` 的 canonical path 是唯一项目边界；即使项目嵌套在更大的 Git 仓库中，也不会把父仓库无关改动收进 Capsule。

V0.1 命令仍可使用：`capture` 或 `resume` 未指定 `--task` 时走项目保留的默认 Task，既有 Capsule 按原样读取，不重写历史文件。

## V0.2 的边界

V0.2 不包含常驻 daemon、后台 Session 自动发现、Textual/Web TUI、云同步、向量检索、跨项目自动召回，也不会自动执行搜索结果、旧命令或 next command。

所有支持 `--json` 的命令都会在 stdout 输出一个可直接解析的固定 envelope；进度和诊断信息不会混入 JSON。运行测试：

```bash
python3 -m pytest -q
```

# Offwork 可信交接 Storyline 与设计框架

## 一句话定位

Offwork 为跨 Session、跨 Agent 的工作提供可信、可审计、可恢复的交接。

它不只保存 Agent 说过什么，而是帮助下一位接手者判断：什么可信、什么已经验证、什么仍然未知、现场是否变化，以及下一步是否可以安全继续。

## Storyline：下班前的可信交接

周五 18:27，一个 Agent 正在修复登录失败问题。它声称已经修复 Token 刷新逻辑、补充测试，并确认测试全部通过。

过去，用户只能相信这段总结，或者让下一个 Session 从头检查。Offwork 的价值，是把一句“相信我”变成一张可核验的交接凭证。

### 1. Agent 声称完成

用户执行 `offwork capture`。Offwork 保存 Agent 或 context 中的声明，但不会把声明自动升级为事实：

```text
Agent claimed:
- 已修复 Token 刷新逻辑
- 已补充测试
- 测试全部通过
```

### 2. Offwork 建立现实底稿

Offwork 在 `--project` 指定的项目边界内采集工作现场：

```text
Observed:
- 修改 4 个项目文件
- 新增 2 个测试文件
- 当前分支 feature/login-fix
- HEAD 仍未变化
```

这些事实只能证明现场发生了变化，不能证明实现正确。

### 3. Offwork 亲自验证

只有 Offwork 实际执行过的验收命令，才会进入自动验证结果：

```text
Verified by Offwork:
- python3 -m pytest tests/auth -q：passed
- Capsule integrity：passed
- Restore readiness：passed
```

没有运行过的命令必须显示为 `not_run`，不能因为 Agent 声称“测试通过”而显示为通过。

### 4. 诚实保留未知

```text
Unknown:
- 旧 Token 的迁移行为尚未确认
- 没有人工代码审核记录
- 未验证生产环境
```

Unknown 不是失败，而是交接时尚未被证据覆盖的事项。Offwork 不推测它们已经完成。

### 5. 新 Session 接手

周一，一个没有上一段对话历史的新 Agent 读取 Receipt。它可以立即判断：

- 当前任务和停留位置；
- 哪些内容只是上一位 Agent 的声明；
- 哪些是 Offwork 观察到的项目事实；
- 哪些命令确实由 Offwork 执行并通过；
- 下一步应该验证旧 Token，而不是重复实现修复。

Offwork 的核心成功标准不是保存了多少历史，而是新 Agent 能否不依赖历史，安全开始正确的第一步。

### 6. 现实已经变化

接手前，用户又修改了一个文件。Receipt 此时应同时表达：

```text
Capsule integrity:
- passed

Workspace freshness:
- changed
- capture 后 auth/token.py 已变化
```

Capsule 没有损坏，只是当前 workspace 已经不同于 capture 时的现场。

### 7. 由人结束交接

用户完成审核后，显式接受或拒绝交接：

```text
Human acceptance:
- accepted
- at: 2026-08-31T09:42:00+08:00
- note: 已审核迁移逻辑，可以继续合并
```

自动测试通过不能代替人工验收。

## 产品边界

Offwork 的核心是：

- 可信交接 Capsule；
- 交接审计 Receipt；
- Evidence 与 Unknowns；
- Workspace freshness；
- 显式人工验收。

Offwork 不成为：

- 全局 Shell 历史记录器；
- Ctrl-R 搜索替代；
- 自动 alias 安装器；
- 自动执行工作流的 Agent 平台。

Automation Opportunity 可以保留结构扩展点，但在缺少可靠数据源时应返回 `unavailable`，不能接管 Shell history、修改 `.zshrc` 或生成危险的可直接执行建议。

## 推荐设计：不可变 Capsule + 动态 Receipt

Receipt 不应成为第二套任务系统，也不应要求重写历史 Capsule。推荐把它实现为由三类数据合成的审计视图：

```text
capture 时的不可变事实
+ StateService 中的后续审计状态
+ 当前 workspace 的只读检查
= 当前 Handoff Receipt
```

这个边界允许 Capsule 保持不可变，同时让 freshness 随现场变化、人工验收在后续显式发生。

### 核心对象

```text
Task
  └── Capsule（capture 时的不可变交接锚点）
        └── Receipt（基于 Capsule 生成的审计视图）
              ├── Agent claims
              ├── Observed workspace
              ├── Auto checks
              ├── Handoff verification
              ├── Unknowns / open loops
              ├── Current freshness
              └── Human acceptance
```

### 真相分层

| 层次 | 回答的问题 | 边界 |
| --- | --- | --- |
| `agent_claimed` | Agent 或 context 声称完成了什么 | 不能自动成为验证事实 |
| `offwork_observed` | capture 时项目现场发生了什么 | 不能证明实现正确 |
| `auto_checked` | Offwork 实际执行了什么、结果如何 | 不能代表人工验收 |
| `handoff_verified` | Capsule 是否完整并具备恢复条件 | 与 workspace freshness 独立 |
| `workspace_freshness` | 当前现场是否仍与 capture 一致 | `changed` 不等于 Capsule 损坏 |
| `human_acceptance` | 用户是否明确接受或拒绝 | 只能由显式操作改变 |

不要使用一个模糊的 `verified` 布尔值覆盖这些状态。

## Receipt 结构草案

```json
{
  "receipt": {
    "task": {
      "task_id": "task-123",
      "title": "修复登录失败",
      "goal": "恢复 Token 刷新行为",
      "revision": 4
    },
    "capsule": {
      "capsule_id": "20260829T...",
      "captured_at": "...",
      "completeness": {
        "status": "complete",
        "missing_information": []
      }
    },
    "agent_claimed": {
      "source": "capture_context",
      "summary": "已实现修复并补充测试",
      "items": []
    },
    "offwork_observed": {
      "project_path": "...",
      "branch": "feature/login-fix",
      "head": "...",
      "dirty_files": [],
      "diff_stat": "..."
    },
    "auto_checked": {
      "status": "passed",
      "commands": [
        {
          "argv": ["python3", "-m", "pytest", "tests/auth", "-q"],
          "status": "passed",
          "exit_code": 0,
          "checked_at": "..."
        }
      ]
    },
    "handoff_verified": {
      "integrity": {"status": "passed"},
      "restore": {"status": "passed"}
    },
    "unknowns": [],
    "open_loops": [],
    "workspace_freshness": {
      "status": "fresh",
      "checked_at": "...",
      "changes": []
    },
    "human_acceptance": {
      "status": "pending",
      "acted_at": null,
      "note": null
    }
  }
}
```

人类可读输出必须从这份结构化对象渲染，避免人类输出与 `--json` 表达不同事实。JSON stdout 继续只输出一个合法 envelope。

## 状态生命周期

### Capture 时封存

- Agent claims；
- 项目现场；
- open loops；
- 明确记录的 Unknowns；
- 当次自动检查结果；
- Capsule integrity 和 restore 结果。

### 查看时重新判断

- Workspace freshness：`fresh | changed | unavailable`。

比较必须限制在 `--project` 边界内，并排除 `.offwork` 自身。嵌套在父 Git 仓库中的项目不得采集父目录的无关变化。

### 显式操作后变化

- Human acceptance：`pending | accepted | rejected`；
- 保存操作时间和可选备注；
- 自动验收命令不能改变人工验收状态。

## CLI 表面

本轮不新增顶级 `receipt` 命令，优先复用现有入口：

```text
offwork capture
  成功后返回 Receipt

offwork task show <task>
  查看完整 Receipt，并重新计算 freshness

offwork resume
  展示精简 Receipt，帮助新 Agent 判断是否安全继续

offwork task accept <task> --note "..."
offwork task reject <task> --note "..."
  显式改变 human_acceptance
```

`task complete` 继续只表示配置的本地条件完成，不等同于 `task accept`。

## 最小实现切面

后续实现优先限制在以下范围：

1. Capsule context 增加兼容性的可选 `agent_claims` 与 `unknowns` 字段；
2. StateService 保存可追踪的人工接受或拒绝状态；
3. 建立单一 Receipt builder，供人类输出和 JSON 共同使用；
4. 使用 capture 时项目快照计算当前 freshness；
5. 在 `capture`、`task show` 和 `resume` 中复用 Receipt；
6. 针对状态边界、manifest 篡改、嵌套 Git 和单一 JSON envelope 增加测试。

不在本轮实现通用事件总线、daemon、TUI、Web UI、Shell history 收集或 Automation Opportunity 分析器。

## Demo 路径

一个完整演示只需要一条可理解的链路：

1. 在临时项目创建 Task；
2. 使用包含 claim、unknown 和 open loop 的 context 执行 capture；
3. 查看 Receipt，确认 claim 与真实验证分离；
4. 修改项目文件；
5. 再次查看 Receipt，确认 integrity 仍通过、freshness 变为 `changed`；
6. 显式执行 accept 或 reject；
7. 查看最终 Receipt，确认时间和备注被记录。

## 产品成功标准

Offwork 的成功不是记录了多少 Session、消息或命令，而是：

- 新 Agent 无需打开旧历史即可理解任务；
- 新 Agent 能区分声明、观察、验证与未知；
- workspace 变化不会被误判为 Capsule 损坏；
- 自动检查不会被误判为人工验收；
- 新 Agent 能安全开始正确的第一步。

最终要交付的不是一份更长的总结，而是一张可核验、可质疑、可恢复的交接凭证。

# Offwork PRD 1.0

**版本：** 1.0  
**状态：** Draft  
**日期：** 2026-08-29  
**产品形态：** 本地优先、零安装、CLI-first 原型

## 1. 产品定义

Offwork 为跨 Session、跨 Agent 的本地工作提供可信、可审计、可恢复的交接。

它不试图保存所有对话，也不替代 Agent、Git、CI 或审批系统。它只解决一个高风险时刻：

> 当前 Agent 停止后，一个没有旧 Session 历史的新 Agent，能否根据当前项目现场和一份结构化交接凭证，判断应该继续、先验证，还是停止，并安全开始正确的第一步。

Offwork 的核心产物是不可变的 **Capsule**，以及由 Capsule、当前 workspace 状态和显式人工动作共同生成的 **Handoff Receipt**。

一句话价值主张：

> 不是证明 Agent 做对了，而是让下一位接手者在继续之前，知道自己究竟在信什么。

## 2. 问题与目标用户

### 2.1 目标用户

首版用户是同时使用一种或多种编码 Agent、在本地 Git 项目中进行跨小时或跨天工作的独立开发者。

典型情境：

- Agent 因上下文耗尽、额度变化、异常或主动切换而停止；
- 工作区存在尚未提交的修改、失败测试或未解决问题；
- 重要判断散落在旧 Session 中；
- 新 Agent 无法区分“上一位 Agent 的声明”和“当前仍然成立的项目事实”。

### 2.2 当前替代方式

- 恢复旧 Session；
- 手工复制聊天摘要；
- 查看 `git status`、`git diff` 和零散 TODO；
- 重新向新 Agent 解释上下文；
- 默认相信上一位 Agent 声称测试通过。

这些方式缺少统一的证据边界，也无法回答 capture 后 workspace 是否已经变化。

### 2.3 产品目标

在一个五分钟原型演示中证明：

1. Agent 声明与 Offwork 实际验证互不混淆；
2. Capsule 完整性与 workspace freshness 独立；
3. Unknowns、open loops 和 next step 能跨 Session 保留；
4. 没有旧历史的新 Agent 能据此选择正确的第一步；
5. 人工验收只能由显式用户操作改变。

## 3. 产品原则

### 3.1 证据分层，不合并状态

Offwork 不提供笼统的 `verified=true`。每类事实必须保留自己的来源和状态。

### 3.2 不推测缺失事实

没有证据的业务结果、生产状态、客户验收、外部环境或人工审核必须进入 Unknowns 或显示为 `unavailable`。

### 3.3 Fail closed

当项目身份、Capsule 完整性或 freshness 无法可靠判断时，Offwork 不输出乐观结论，也不自动继续下一步。

### 3.4 只提供交接证据，不接管执行

Offwork 不自动运行 `next_step`，不启动或控制 Agent，不恢复、覆盖、stash 或提交用户文件。

### 3.5 一个事实源，两种表现

人类可读 Receipt 和 JSON 输出必须由同一个结构化对象渲染，不维护第二套事实。

### 3.6 产品开发始终保留人类 PM 评审

Offwork 的开发过程必须持续显示一个人类 PM 评审节点。Agent、多 Agent 审核和自动测试可以准备评审证据，但不能代替产品负责人作出接受、拒绝或范围决定。

- 每个里程碑默认评审状态为 `pending`；
- 只有用户或被明确指定的人类 PM 的显式决定可以改为 `accepted`、`rejected` 或 `changes_requested`；
- commit、push、测试通过和技术审核不自动改变 PM 评审状态；
- 里程碑是否完成、是否可合并以及下一批范围均由当前 PM 评审记录约束；
- 该开发评审与 Capsule 的 `human_acceptance` 相互独立，不能互相推导。

当前评审状态、证据和决定记录在 [`PM_REVIEW.md`](./PM_REVIEW.md)。

## 4. V1.0 范围

### 4.1 Included

- 显式且可验证的项目边界；
- 最小 Task 身份、目标和 revision；
- 每次 capture 生成一个不可变 Capsule；
- 结构化 Handoff Receipt；
- Agent claims、Offwork observations、自动检查和人工验收分离；
- Evidence、Unknowns、open loops 和 next step；
- 项目边界内的 Git workspace freshness；
- Capsule integrity、completeness 和落盘后 restore round-trip；
- 显式、Capsule 级的人工接受或拒绝；
- 稳定的 JSON envelope；
- 人类输出与 JSON 输出的事实一致性；
- 无历史新 Session 的手工恢复验证。

### 4.2 Out of scope

- Agent 启动、停止、调度或远程控制；
- daemon、TUI、Web UI、云同步和远程控制面；
- Shell history、Ctrl-R 替代、alias 或 `.zshrc` 修改；
- 自动执行 next step 或工作流；
- Session transcript 和全量 Agent observability；
- Task 看板、依赖编排、全局项目注册和持久记忆；
- Automation Opportunity 分析或脚本建议；
- OS 级 sandbox；
- 客户验收、生产发布、法律审批或合规认证；
- 独立身份认证、可信时间戳、电子签名或防恶意本地管理员篡改。

## 5. 核心概念与状态边界

| 概念 | 含义 | 状态或形式 |
| --- | --- | --- |
| `agent_claimed` | capture context 中由 Agent 提供的声明 | 结构化文本，永远不是 pass/fail |
| `offwork_observed` | Offwork 在显式项目边界内实际采集的现场 | 结构化快照 |
| `auto_checked` | Offwork 在 capture 中真实尝试运行的检查 | `not_run / passed / failed / unavailable` |
| `handoff_verified.integrity` | Capsule 固定成员与 manifest 一致 | `passed / failed` |
| `handoff_verified.completeness` | 必需交接字段存在且可解析 | `complete / incomplete` |
| `handoff_verified.restore` | 从已发布 Capsule 重新加载并重建规范 Receipt 成功 | `passed / failed` |
| `workspace_freshness` | 当前项目快照相对 capture 时的变化 | `fresh / changed / unavailable` |
| `human_acceptance` | 用户对某个明确 Capsule 的显式决定 | `pending / accepted / rejected` |

边界说明：

- `auto_checked=passed` 不表示人工验收、客户验收或生产可用；
- `human_acceptance=accepted` 不表示 PR 审批、电子签名或发布授权；
- `integrity=passed` 只表示本地文件自洽，不表示独立身份或不可抵赖；
- `freshness=fresh` 只覆盖本 PRD 定义的项目内 Git 快照；
- ignored files、外部服务、数据库、环境变量和生产状态不在 freshness 范围内。

## 6. 核心用户流程

```text
初始化项目
  → 创建 Task 与检查命令
  → Agent 提供 capture context
  → Offwork 执行检查并采集最终项目快照
  → 发布不可变 Capsule
  → 从落盘 Capsule 重建 Receipt
  → 新 Agent 查看指定 Capsule
  → 比较当前 workspace freshness
  → 新 Agent 决定继续、先验证或停止
  → 用户显式接受或拒绝该 Capsule
```

### 6.1 只读命令的目标规则

- `capture` 返回新 `capsule_id` 和捕获时的 `task_revision`；
- `task show` 和 `resume` 可以默认展示 Task 的最新 Capsule，但输出必须明确显示实际使用的 `capsule_id`；
- 两个命令都允许通过 `--capsule CAPSULE_ID` 查看历史 Capsule；
- 不得仅按时间戳猜测目标，最新 Capsule 必须由持久化关系明确解析。

### 6.2 写命令的目标规则

人工接受或拒绝必须同时指定：

- `TASK_ID`；
- `--capsule CAPSULE_ID`；
- `--if-revision N`，其中 N 来自用户实际查看的 Receipt。

若 Task revision 已变化，操作必须失败并要求用户重新查看 Receipt。系统不得自动改为接受最新 Capsule。

## 7. CLI 需求

```text
offwork init --project PATH [--json]

offwork task add TITLE \
  --goal GOAL \
  [--check COMMAND]... \
  --project PATH [--json]

offwork capture \
  --task TASK_ID \
  --context CONTEXT.json \
  --project PATH [--json]

offwork task show TASK_ID \
  [--capsule CAPSULE_ID] \
  --project PATH [--json]

offwork resume \
  --task TASK_ID \
  [--capsule CAPSULE_ID] \
  --project PATH [--json]

offwork task accept TASK_ID \
  --capsule CAPSULE_ID \
  --if-revision N \
  [--note TEXT] \
  --project PATH [--json]

offwork task reject TASK_ID \
  --capsule CAPSULE_ID \
  --if-revision N \
  [--note TEXT] \
  --project PATH [--json]
```

不增加顶级 `receipt` 命令。`capture`、`task show` 和 `resume` 复用同一个 Receipt builder。

`resume` 只展示交接事实和安全下一步，不执行 `next_step`，也不改变 workspace。

## 8. Capture context 合同

```json
{
  "summary": "已实现 Token 刷新修复并补充测试",
  "agent_claims": [
    "登录失败已经修复",
    "测试全部通过"
  ],
  "unknowns": [
    "旧 Token 迁移行为尚未确认"
  ],
  "open_loops": [
    {
      "title": "确认旧 Token 的迁移行为",
      "disposition": "resolve",
      "note": "先运行迁移测试"
    }
  ],
  "next_step": "运行旧 Token 迁移测试"
}
```

要求：

- `summary` 和 `next_step` 必填；
- `agent_claims`、`unknowns`、`open_loops` 可以为空数组；
- Offwork 不推导缺失的业务结论；
- `next_step` 仅作为文本保存，任何读取命令都不得执行它；
- capture context 不保存完整 Session transcript。

## 9. Receipt 1.0 合同

Receipt 至少必须回答：

1. 当前 Task 是什么；
2. Agent 声称完成了什么；
3. Offwork 实际观察到什么项目变化；
4. Offwork 亲自运行并验证了什么；
5. 哪些事项未知或未经验证；
6. 有哪些 open loops 和下一步；
7. Capsule 是否完整并能从落盘状态恢复；
8. 当前 workspace 是否已经变化；
9. 用户是否明确接受。

最小结构：

```json
{
  "schema_version": "offwork.receipt/v1",
  "task": {
    "task_id": "task-...",
    "title": "修复登录失败",
    "goal": "恢复 Token 刷新行为",
    "current_revision": 3,
    "captured_revision": 2
  },
  "capsule": {
    "capsule_id": "capsule-...",
    "captured_at": "2026-08-29T00:00:00+00:00"
  },
  "agent_claimed": {
    "source": "capture_context",
    "summary": "已实现 Token 刷新修复并补充测试",
    "items": ["登录失败已经修复", "测试全部通过"]
  },
  "offwork_observed": {
    "project_id": "project-...",
    "project_path": "/absolute/project",
    "git_root": "/absolute/parent-or-project",
    "branch": "feature/login-fix",
    "head": "0123456789abcdef",
    "changed_paths": ["auth/token.py"]
  },
  "auto_checked": {
    "status": "passed",
    "checks": [
      {
        "command": "python3 -m unittest",
        "argv": ["python3", "-m", "unittest"],
        "cwd": "/absolute/project",
        "status": "passed",
        "returncode": 0,
        "started_at": "2026-08-29T00:00:01+00:00",
        "finished_at": "2026-08-29T00:00:02+00:00"
      }
    ]
  },
  "handoff_verified": {
    "integrity": {"status": "passed"},
    "completeness": {"status": "complete", "missing_information": []},
    "restore": {"status": "passed"}
  },
  "unknowns": ["旧 Token 迁移行为尚未确认"],
  "open_loops": [
    {
      "title": "确认旧 Token 的迁移行为",
      "disposition": "resolve",
      "note": "先运行迁移测试"
    }
  ],
  "next_step": "运行旧 Token 迁移测试",
  "workspace_freshness": {
    "status": "fresh",
    "scope": "explicit_git_project",
    "checked_at": "2026-08-29T00:00:03+00:00",
    "changes": [],
    "limitations": ["ignored files and external state are not checked"]
  },
  "human_acceptance": {
    "status": "pending",
    "acted_at": null,
    "note": null
  }
}
```

### 9.1 人类输出

人类输出从上述对象渲染，至少包含以下同名事实区块：

- Task；
- Agent claimed；
- Observed by Offwork；
- Verified by Offwork；
- Unknowns；
- Open loops；
- Next step；
- Capsule integrity/completeness/restore；
- Workspace freshness；
- Human acceptance；
- Capsule ID 和当前 Task revision。

本地不可信文本中的换行、控制字符和 ANSI escape 必须以可见形式转义，避免伪造 Receipt 状态。

## 10. 自动检查

### 10.1 执行边界

- 检查只在 `capture` 中运行；
- `show`、`resume`、`accept` 和 `reject` 不运行检查；
- 外部命令始终使用 argv 和 `shell=False`；
- `--check` 字符串解析为 argv，不解释管道、重定向或 `&&`；
- cwd 固定为 canonical project path；
- 检查命令是用户显式配置并授权的本地代码，不是 sandbox；
- V1.0 不承诺阻止检查命令访问网络、环境变量或项目外文件；
- 不保存 stdout/stderr 原文，仅记录状态、argv、返回码和时间，以降低凭证泄露风险；
- 每项检查和整次 capture 都必须有固定超时，超时后终止检查进程。

### 10.2 状态聚合

- 零个已配置检查：`not_run`；
- 非空检查全部实际执行、未超时且返回 0：`passed`；
- 任一已执行检查返回非零：`failed`；
- 任一检查无法启动、超时或未能完成：`unavailable`；
- `unavailable` 和 `failed` 都不得被其他成功检查覆盖。

### 10.3 Capture 顺序

```text
校验项目和 Task
  → 解析 capture context
  → 执行检查
  → 采集检查结束后的最终 workspace snapshot
  → 写入 staging Capsule
  → 发布 Capsule
  → 从落盘 Capsule执行 restore round-trip
  → 生成 Receipt
```

检查若改变工作区，改变后的现场属于本次 capture snapshot，避免 capture 刚结束就因检查副作用显示 `changed`。

## 11. Workspace freshness

### 11.1 项目边界

- freshness 只比较 `--project` 的 canonical path；
- Git 根目录仅作为 metadata；
- 项目嵌套于父 Git 仓库时，父仓库中项目外的文件和 commit 不得改变结果；
- 所有 Git worktree 查询必须在命令层使用项目 pathspec，而不是扫描整个父仓库后再过滤；
- `.offwork/` 始终排除；
- 不自动修改、恢复、stash 或提交 workspace。

### 11.2 Capture snapshot

V1.0 为显式项目边界内的以下内容记录稳定 fingerprint：

- project identity 和 canonical path；
- branch 与完整 HEAD，作为观察 metadata；
- 项目内 tracked regular files 和 symlinks；
- 项目内 untracked regular files 和 symlinks；
- deleted、renamed 和 type-changed paths；
- 文件类型、mode、path 和内容或 symlink target 的 SHA-256。

ignored files 不纳入 snapshot，并在 Receipt limitations 中明确显示。遇到 submodule、gitlink、嵌套 `.git`、不可读路径或扫描过程中的并发变化时返回 `unavailable`，不得猜测为 fresh。

对于嵌套项目，父仓库全局 HEAD 只显示为 metadata，不参与 freshness 判断；项目外 commit 后，如果项目内 snapshot 未变，结果仍为 `fresh`。

### 11.3 状态定义

- `fresh`：当前可靠 snapshot 与 capture snapshot 在规定范围内完全一致；
- `changed`：两个 snapshot 都可靠，但项目范围内存在差异；
- `unavailable`：无法可靠获取或比较规定范围内的 snapshot。

`changed` 永远不改变 Capsule integrity。

## 12. Capsule 完整性与恢复

### 12.1 固定内容

每个 Capsule 目录只允许以下固定成员：

```text
capsule.json
checks.json
restore-test.json
manifest.json
```

每个文件都具有明确的 schema version。Manifest 记录三个 payload 文件的固定 basename、原始字节长度和 SHA-256；SQLite 保存 `manifest.json` 原始字节的 SHA-256。

读取顺序必须为：

1. 从格式已验证的 Capsule ID 推导项目内路径；
2. 拒绝绝对路径、`..`、未知成员和 symlink；
3. 对照 SQLite 验证 manifest 原始字节 hash；
4. 验证 manifest schema 与固定成员；
5. 验证 payload 原始字节；
6. 最后解析 JSON 并生成 Receipt。

Integrity failure 时不得继续 freshness 判断或输出正常 Receipt。

### 12.2 发布与崩溃恢复

Capsule 发布遵循：

```text
私有 staging
  → 完整写入并 fsync 文件和 staging 目录
  → 原子 rename 到 capsules/<capsule-id>
  → fsync capsules 父目录
  → SQLite 注册 Capsule
```

数据库不得在 Capsule 目录 durable 之前暴露有效行。启动或读取时需要幂等对账：完整且校验通过但尚未注册的 Capsule可以补登记；不完整 staging 不得作为 Capsule 使用。

`handoff_verified.restore=passed` 表示 Offwork 已从正式发布目录重新读取 Capsule，并成功重建规范 Receipt 投影，而不是仅复用 capture 时的内存对象。

## 13. 人工验收

- 每个 Capsule 默认 `pending`；
- 只有显式 `task accept` 或 `task reject` 可以改变状态；
- 自动检查、capture、show 和 resume 永远不能改变人工验收；
- 每次操作保存 Capsule ID、时间、可选备注和操作后的 Task revision；
- 状态事件追加写，不重写 Capsule；
- 后续显式改判可以追加新事件，Receipt 以最高 Task revision 的有效事件为当前状态；
- 写入必须在同一事务中执行 revision compare-and-swap；
- Capsule ID 与 Task ID 必须在数据库层保持一致，不能交叉绑定。

## 14. JSON envelope

所有 `--json` 命令的 stdout 必须且只能包含一个合法 JSON 对象。子进程输出和诊断不得进入 stdout。

成功：

```json
{
  "schema_version": "offwork.cli/v1",
  "ok": true,
  "command": "task.show",
  "data": {}
}
```

失败：

```json
{
  "schema_version": "offwork.cli/v1",
  "ok": false,
  "command": "task.show",
  "error": {
    "code": "CAPSULE_INTEGRITY_FAILED",
    "message": "Capsule integrity verification failed",
    "details": {
      "capsule_id": "capsule-...",
      "integrity": "failed",
      "freshness": "not_evaluated"
    }
  }
}
```

要求：

- 参数错误、项目不存在、Task/Capsule 不存在、revision conflict 和 integrity failure 都有稳定 error code；
- 失败返回非零退出码；
- manifest 篡改返回 `CAPSULE_INTEGRITY_FAILED`，不得伪装成 `workspace_freshness=changed`；
- human 模式表达相同的错误码、目标 Capsule 和失败事实；
- diagnostics 只写 stderr。

## 15. 功能需求

| ID | 需求 | 优先级 |
| --- | --- | --- |
| FR-01 | 用户可在明确项目路径初始化私有 Offwork 状态 | P0 |
| FR-02 | 用户可创建包含 goal 与零个或多个 checks 的 Task | P0 |
| FR-03 | capture 从结构化 context 生成不可变 Capsule | P0 |
| FR-04 | Receipt 保留 claims、Unknowns、open loops 和 next step | P0 |
| FR-05 | Offwork 只把实际执行的检查记入 `auto_checked` | P0 |
| FR-06 | 用户可查看 Task 最新或显式指定的 Capsule | P0 |
| FR-07 | freshness 仅反映显式项目边界内变化 | P0 |
| FR-08 | integrity failure 与 workspace changed 独立表达 | P0 |
| FR-09 | 人工验收绑定明确 Capsule 和用户所见 revision | P0 |
| FR-10 | human 与 JSON 输出来自同一 Receipt 对象 | P0 |
| FR-11 | 所有 JSON stdout 只有一个稳定 envelope | P0 |
| FR-12 | 无历史新 Agent 能仅凭 Receipt 做出预期第一决策 | P0，手工原型验收 |

## 16. 非功能需求

### 16.1 兼容与依赖

- Python 3.9+；
- 仅使用标准库和系统 Git；
- 不增加生产依赖；
- 不复制历史 `offwork-capsule` 源码或测试；
- 历史 Capsule 兼容不属于 V1.0 原型验收，除非实现开始前提供明确 fixture 和版本合同。

### 16.2 权限与路径

- `.offwork/` 和 Capsule 目录创建为 `0700`；
- state、JSON、manifest、SQLite 辅助文件、staging 和锁文件创建为 `0600`；
- 复用已有 `.offwork` 前验证 owner、类型、mode 和 symlink；
- 所有 Capsule archive path 使用 `.offwork` 内的规范相对路径；
- 不跟随固定目录或 Capsule 成员 symlink。

### 16.3 可靠性

- Task mutation 使用 SQLite 事务；
- Receipt 的 Task、Capsule 和 acceptance 读取使用同一一致性快照；
- SQLite 每个连接启用 foreign keys；
- 数据库和所有持久化 JSON 都有 schema version；
- capture 失败不得留下可读取但不完整的 Capsule。

### 16.4 零安装

从项目仓库外的任意 cwd，用户可以直接运行 `bin/offwork --help`、`--version` 和完整演示，不需要安装第三方包。

## 17. 五分钟原型 Storyline

演示项目只有一个 Task：“修复登录失败”。

1. 初始化临时 Git 项目并创建 Task；
2. 配置一个真实检查命令；
3. 提供 Agent context，其中声明“测试全部通过”，同时保留旧 Token 迁移 Unknown 和 next step；
4. 执行 capture，显示具体 Capsule ID；
5. Receipt 分别显示 Agent claim、项目观察和 Offwork 实际检查；
6. 显示人工验收仍为 `pending`；
7. 修改项目内文件；
8. 再次查看同一 Capsule：integrity 与 restore 仍通过，freshness 为 `changed`；
9. 开启无旧历史的新 Session，只提供项目路径和该 Capsule 的 `resume --json` 输出；
10. 新 Agent 必须选择“先验证”，引用 Unknown/open loop，并给出 Receipt 中的 next step；
11. 用户通过 Capsule ID 和 revision 显式 accept 或 reject；
12. 最终 human 与 JSON Receipt 显示相同事实。

为了直观证明 claim 不会制造检查结果，演示至少包含一次可控反例：Agent 声称测试通过，但 Offwork 实际检查返回非零或 unavailable；Receipt 必须同时保留声明和真实检查状态。

## 18. 原型验收标准

- [ ] Agent claim 与 Offwork verification 使用不同字段；
- [ ] 零配置 check 为 `not_run`，未完成的 check 永远不是 `passed`；
- [ ] Unknowns、open loops 和 next step 跨 capture/resume 不丢失；
- [ ] 同一 Task 的多个 Capsule 可被明确寻址；
- [ ] stale revision 的 accept/reject 被拒绝；
- [ ] Capsule 完整但 workspace 已变时，integrity/restore 通过且 freshness 为 `changed`；
- [ ] manifest 篡改返回 integrity failure，并跳过 freshness；
- [ ] 父 Git 仓库项目外 dirty change 和 commit 不影响嵌套项目 freshness；
- [ ] 不可可靠比较的项目返回 `unavailable`；
- [ ] 人工验收默认为 `pending`；
- [ ] 自动检查通过不会设置 human accepted；
- [ ] 接受、拒绝和后续改判只由显式命令发生；
- [ ] human 和 JSON 表达相同的规范事实集合；
- [ ] JSON stdout 始终只有一个合法 envelope；
- [ ] timeout、spawn failure 和多检查混合结果符合状态合同；
- [ ] `resume` 不执行 next step、不改变 HEAD/index/项目文件；
- [ ] permissions、symlink、路径逃逸和发布中断测试通过；
- [ ] 无历史新 Agent 在 changed + Unknown 场景中选择“先验证”并引用正确 next step；
- [ ] 完整标准库测试套件与 `compileall` 通过；
- [ ] 全部演示命令在新的临时项目中于五分钟内完成。

## 19. 原型成功指标

本版本评估原型是否诚实成立，不设置市场验证或增长门槛。

| 指标 | 目标 |
| --- | --- |
| 五分钟完整演示 | 成功完成 |
| Receipt 必需事实保留率 | 100% |
| 已知 stale workspace 识别率 | 100% 测试场景 |
| 错误 human acceptance 绑定 | 0 |
| 自动检查与 Agent claim 混淆 | 0 |
| 无历史 Agent 正确第一决策 | 通过预设 continue/verify/stop 反例 |
| 原型对用户项目的非授权写操作 | 0 |

这些指标不证明市场需求、客户接受、生产可靠性或合规性。

## 20. 实施优先级

1. 项目边界、稳定 JSON envelope 和状态存储；
2. Task、Capsule 固定格式与可靠发布；
3. Receipt 无损合同及 human/JSON 双渲染；
4. 项目限定 freshness 与 integrity failure；
5. Capsule 级 human acceptance 和 revision CAS；
6. 五分钟端到端演示与 fresh-agent 黑盒验收。

每个阶段只测试该阶段已经实现的状态。最后的端到端测试是集成回归，前序功能正确时应直接通过，不强制制造 RED。

## 21. 风险与缓解

| 风险 | 影响 | V1.0 缓解 |
| --- | --- | --- |
| Receipt 只是结构完整，内容建议仍可能错误 | 新 Agent执行错误第一步 | Unknowns、freshness、禁止自动执行、fresh-agent 黑盒反例 |
| 本地用户可同时修改 Capsule 和数据库 | 不能宣称防恶意篡改 | 明确只保证本地自洽，不宣称签名或不可抵赖 |
| Check 是任意本地程序 | 可访问项目外资源 | 仅运行用户显式配置的 check，明确非 sandbox，限制时长和输出 |
| 父 Git 仓库污染子项目判断 | freshness 误报 | Git pathspec、项目内 fingerprint、父 HEAD 仅作 metadata |
| Capture 在文件发布与 DB 注册间崩溃 | 孤儿或悬空记录 | durable publication 顺序与幂等对账 |
| 人工操作针对旧 Receipt | 接受错误 Capsule | Capsule ID + expected revision CAS |
| 输出含控制字符 | 伪造终端显示 | human renderer 可见转义 |
| 范围扩张为 Agent 平台 | 原型无法按时完成 | 严格执行 Out of scope 和 stop conditions |

## 22. Stop conditions

实现过程中遇到以下情况必须暂停并重新确认范围：

- 需要新增生产依赖；
- 需要 daemon、Web/TUI、云服务或 Agent 控制器；
- 需要削弱项目边界、symlink 或 fail-closed 规则；
- 需要自动执行 next step、自动恢复或修改用户 workspace；
- 需要重写已经发布的 Capsule；
- 试图把自动检查、人类接受、客户验收或生产发布合并；
- 试图在 V1.0 中加入 Automation Opportunity、Shell history 或工作流执行。

## 23. V1.0 完成定义

PRD 1.0 的产品原型完成，不等于正式发布。完成条件是：

> 在一个新的临时 Git 项目中，用户能于五分钟内创建 Task、capture 一个不可变 Capsule、查看结构化 Receipt、发现 capture 后的项目变化，并让一个没有旧 Session 历史的新 Agent选择正确的安全第一步；随后用户能对明确的 Capsule 做出可追踪的显式接受或拒绝。

除此之外的能力不是 V1.0 完成条件。

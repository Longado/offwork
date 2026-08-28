from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Sequence

from .capsule import CapsuleValidationError


class VerifierUnavailableError(RuntimeError):
    """Raised when an explicitly requested external verifier is unavailable."""


RESTORE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "ready_to_resume": {"type": "boolean"},
        "understood_goal": {"type": "string"},
        "current_state": {"type": "string"},
        "next_action": {"type": "string"},
        "missing_information": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": [
        "ready_to_resume",
        "understood_goal",
        "current_state",
        "next_action",
        "missing_information",
    ],
    "additionalProperties": False,
}


def _normalize_agent_test(value: Dict[str, Any]) -> Dict[str, Any]:
    missing_keys = [key for key in RESTORE_SCHEMA["required"] if key not in value]
    if missing_keys:
        raise CapsuleValidationError(
            "恢复测试返回缺少字段：" + "、".join(missing_keys)
        )
    missing = value.get("missing_information")
    if not isinstance(missing, list) or any(not isinstance(item, str) for item in missing):
        raise CapsuleValidationError("恢复测试的 missing_information 格式错误")
    ready = value.get("ready_to_resume")
    if not isinstance(ready, bool):
        raise CapsuleValidationError("恢复测试的 ready_to_resume 格式错误")
    return {
        "mode": "fresh-agent",
        "ready_to_resume": ready and not missing,
        "understood_goal": str(value.get("understood_goal", "")).strip(),
        "current_state": str(value.get("current_state", "")).strip(),
        "next_action": str(value.get("next_action", "")).strip(),
        "missing_information": [item.strip() for item in missing if item.strip()],
    }


def run_command_verifier(
    capsule: Dict[str, Any], command: Sequence[str], timeout: int = 120
) -> Dict[str, Any]:
    payload = json.dumps({"capsule": capsule}, ensure_ascii=False)
    result = subprocess.run(
        list(command),
        input=payload,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "未知错误"
        raise CapsuleValidationError("独立恢复测试执行失败：%s" % detail)
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise CapsuleValidationError("独立恢复测试没有返回有效 JSON") from error
    if not isinstance(response, dict):
        raise CapsuleValidationError("独立恢复测试必须返回 JSON 对象")
    return _normalize_agent_test(response)


def parse_claude_result(output: str) -> Dict[str, Any]:
    try:
        wrapper = json.loads(output)
    except json.JSONDecodeError as error:
        raise CapsuleValidationError("Claude 恢复测试没有返回有效 JSON") from error
    if not isinstance(wrapper, dict):
        raise CapsuleValidationError("Claude 恢复测试返回格式错误")
    structured = wrapper.get("structured_output")
    if not isinstance(structured, dict):
        raw_result = wrapper.get("result")
        if isinstance(raw_result, str):
            try:
                structured = json.loads(raw_result)
            except json.JSONDecodeError:
                structured = None
    if not isinstance(structured, dict):
        raise CapsuleValidationError("Claude 恢复测试缺少 structured_output")
    return _normalize_agent_test(structured)


def build_restore_prompt(capsule: Dict[str, Any]) -> str:
    return """你是一个没有任何历史会话的恢复测试 Agent。下面只有一枚 Work Capsule。
请仅判断新的工作会话能否安全地开始第一步，而不是仅凭胶囊完成整个任务。

通过标准：
- 能准确复述当前目标和停留位置。
- 能指出一个具体、无歧义且不会破坏现场的下一步。
- 胶囊不存在会让第一步走错方向的矛盾或关键空白。

不要因为后续仍需联系他人、查询资料或打开项目文件而判定失败；这些可以是下一步本身。
只有连第一步做什么、找谁、处理哪个对象都无法判断时，才写入 missing_information。
不要访问文件、网络或其他上下文，也不要假设胶囊之外的信息。

WORK CAPSULE:
%s
""" % json.dumps(capsule, ensure_ascii=False, indent=2)


def run_claude_verifier(capsule: Dict[str, Any], timeout: int = 120) -> Dict[str, Any]:
    executable = shutil.which("claude")
    if not executable:
        raise VerifierUnavailableError("未找到 claude CLI，无法运行失忆 Agent 测试")
    prompt = build_restore_prompt(capsule)
    command = [
        executable,
        "-p",
        "--safe-mode",
        "--tools",
        "",
        "--no-session-persistence",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(RESTORE_SCHEMA, ensure_ascii=False),
    ]
    env = os.environ.copy()
    for key in list(env):
        if key == "CLAUDECODE" or key.startswith("CLAUDE_CODE_ENTRYPOINT"):
            env.pop(key, None)
    try:
        result = subprocess.run(
            command,
            input=prompt,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=env,
        )
    except (FileNotFoundError, PermissionError) as error:
        raise VerifierUnavailableError(
            "claude CLI 无法启动，fresh verifier capability unavailable"
        ) from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "未知错误"
        raise CapsuleValidationError("Claude 恢复测试执行失败：%s" % detail)
    return parse_claude_result(result.stdout)


def validate_first_step_feasibility(
    capsule: Dict[str, Any], project_root: Path
) -> Dict[str, Any]:
    """Check explicit local command prerequisites without executing the command."""

    missing: List[str] = []
    command_text = str(capsule.get("next_command", "")).strip()
    if command_text:
        try:
            arguments = shlex.split(command_text)
        except ValueError:
            arguments = []
            missing.append("建议命令无法安全解析")

        root = Path(project_root).resolve()
        if arguments:
            executable = arguments[0]
            if "/" in executable:
                _check_project_command_path(root, executable, missing)
            elif shutil.which(executable) is None:
                missing.append("建议命令不可用：%s" % executable)

            for argument in arguments[1:]:
                if argument.startswith("-") or "://" in argument:
                    continue
                if "/" in argument or argument.startswith("."):
                    _check_project_command_path(root, argument, missing)

    return {
        "mode": "first-step-preflight",
        "ready_to_resume": not missing,
        "understood_goal": str(capsule.get("goal", "")),
        "current_state": str(capsule.get("summary", "")),
        "next_action": str(capsule.get("next_step", "")),
        "missing_information": missing,
    }


def _check_project_command_path(
    project_root: Path, value: str, missing: List[str]
) -> None:
    path = Path(value)
    candidate = path if path.is_absolute() else project_root / path
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(project_root)
    except ValueError:
        missing.append("建议命令路径超出项目边界：%s" % value)
        return
    if not candidate.exists():
        missing.append("建议命令引用的项目路径不存在：%s" % value)


def merge_restore_tests(
    local_test: Dict[str, Any], agent_test: Dict[str, Any]
) -> Dict[str, Any]:
    missing: List[str] = []
    for source in (local_test, agent_test):
        for item in source.get("missing_information", []):
            if item not in missing:
                missing.append(item)
    return {
        "mode": "local+%s" % agent_test.get("mode", "fresh-agent"),
        "ready_to_resume": bool(
            local_test.get("ready_to_resume")
            and agent_test.get("ready_to_resume")
            and not missing
        ),
        "understood_goal": agent_test.get("understood_goal", ""),
        "current_state": agent_test.get("current_state", ""),
        "next_action": agent_test.get("next_action", ""),
        "missing_information": missing,
    }

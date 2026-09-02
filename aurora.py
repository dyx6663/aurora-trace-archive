"""AURORA TRACE: an explainable local coding-agent demo.

Only Python's standard library is used. The implementation intentionally keeps
the important Agent logic visible: context, tools, parsing, execution, loop,
guardrails, and termination.
"""

from __future__ import annotations

import difflib
import io
import json
import os
import shlex
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import default as email_default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
SEED = ROOT / "seed_project"
PORT = int(os.getenv("AURORA_PORT", "8765"))
RUNS: dict[str, dict[str, Any]] = {}
PROJECTS: dict[str, dict[str, Any]] = {
    "demo": {"id": "demo", "name": "Todo Boundary Demo", "path": SEED,
             "source": "built-in", "file_count": 2}
}
RUN_LOCK = threading.Lock()
PROJECT_LOCK = threading.Lock()
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
PROJECT_INDEX = ROOT / "projects.json"
IGNORED_DIRS = {"__pycache__", ".git", ".idea", ".vscode", ".venv", "venv", "node_modules", "dist", "build"}
RUN_SCHEMA_VERSION = 2
MAX_CONTEXT_CHARS = 24000
TASK_TYPES = {"repair", "feature", "refactor", "change"}
APPROVAL_MODES = {"auto", "manual"}
TERMINAL_STATES = {"COMPLETED", "FAILED", "CANCELLED"}


class RunCancelled(Exception):
    """Cooperative cancellation signal used between Agent control steps."""


def now() -> str:
    return time.strftime("%H:%M:%S")


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def infer_task_type(task: str, requested: str | None = None) -> str:
    """Classify the contract without asking the model to self-certify it."""
    if requested in TASK_TYPES:
        return requested
    text = (task or "").lower()
    if any(token in text for token in ("bug", "fix", "debug", "故障", "错误", "缺陷", "修复")):
        return "repair"
    if any(token in text for token in ("refactor", "重构", "重写", "整理结构", "迁移")):
        return "refactor"
    if any(token in text for token in ("feature", "implement", "add ", "新增", "增加", "添加", "实现", "支持")):
        return "feature"
    return "change"


def safe_path(workspace: Path, relative: str) -> Path:
    candidate = (workspace / relative).resolve()
    workspace = workspace.resolve()
    if candidate != workspace and workspace not in candidate.parents:
        raise ValueError("path escapes the isolated workspace")
    return candidate


def safe_web_path(relative: str) -> Path:
    """Resolve static assets without allowing the URL to escape web/."""
    candidate = (WEB / unquote(relative)).resolve()
    if candidate != WEB.resolve() and WEB.resolve() not in candidate.parents:
        raise ValueError("static asset escapes web root")
    return candidate


def safe_zip_member(name: str) -> Path:
    """Validate a ZIP member before extraction; return a normalized relative path."""
    normalized = name.replace("\\", "/")
    relative = Path(normalized)
    if not normalized or relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"unsafe archive path: {name}")
    if len(normalized) > 240 or (len(normalized) > 1 and normalized[1] == ":"):
        raise ValueError(f"unsafe archive path: {name}")
    return relative


def import_zip_project(filename: str, data: bytes) -> dict[str, Any]:
    """Import a small ZIP project into a new immutable-once-created project folder."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("ZIP exceeds the 10 MB upload limit")
    if not filename.lower().endswith(".zip"):
        raise ValueError("only .zip project archives are supported")
    project_id = "p-" + uuid.uuid4().hex[:8]
    destination = ROOT / "projects" / project_id
    files: list[tuple[Path, bytes]] = []
    total_uncompressed = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if len(archive.infolist()) > 500:
                raise ValueError("archive contains too many entries")
            for item in archive.infolist():
                relative = safe_zip_member(item.filename)
                if item.is_dir():
                    continue
                total_uncompressed += item.file_size
                if total_uncompressed > 30 * 1024 * 1024:
                    raise ValueError("uncompressed project exceeds the 30 MB limit")
                files.append((relative, archive.read(item)))
    except zipfile.BadZipFile as exc:
        raise ValueError("uploaded file is not a valid ZIP archive") from exc
    if not files:
        raise ValueError("archive contains no files")
    destination.mkdir(parents=True, exist_ok=False)
    for relative, content in files:
        target = safe_path(destination, str(relative))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    top_dirs = {path.parts[0] for path, _ in files if len(path.parts) > 1}
    source = destination / next(iter(top_dirs)) if len(top_dirs) == 1 and all(path.parts[0] == next(iter(top_dirs)) for path, _ in files) else destination
    profile = profile_project(source)
    project = {"id": project_id, "name": Path(filename).stem,
               "path": source, "source": "uploaded", "file_count": len(files),
               "profile": profile}
    with PROJECT_LOCK:
        PROJECTS[project_id] = project
        save_project_index()
    return {k: str(v) if isinstance(v, Path) else v for k, v in project.items() if k != "path"}


def profile_project(root: Path) -> dict[str, Any]:
    extensions: dict[str, int] = {}
    names: set[str] = set()
    file_count = 0
    for item in root.rglob("*"):
        relative = item.relative_to(root)
        if not item.is_file() or any(part in IGNORED_DIRS for part in relative.parts):
            continue
        file_count += 1
        names.add(item.name.lower())
        extension = item.suffix.lower() or "[no extension]"
        extensions[extension] = extensions.get(extension, 0) + 1
    languages = {".py": "Python", ".js": "JavaScript", ".ts": "TypeScript",
                 ".java": "Java", ".go": "Go", ".rs": "Rust", ".cpp": "C++"}
    detected = [languages[ext] for ext, _ in sorted(extensions.items(), key=lambda pair: -pair[1]) if ext in languages][:3]
    commands = []
    if "pytest.ini" in names:
        commands.append("python -m pytest")
    elif "tests" in {part.lower() for item in root.rglob("*") for part in item.relative_to(root).parts}:
        commands.append("python -m unittest discover -s tests -v")
    elif ".py" in extensions:
        commands.append("python -m unittest discover -v")
    if "package.json" in names:
        commands.append("npm test")
    return {"files": file_count, "languages": detected or ["Unknown"],
            "suggested_tests": commands or ["No test command detected"]}


def save_project_index() -> None:
    records = []
    for project in PROJECTS.values():
        if project["id"] == "demo":
            continue
        record = {k: v for k, v in project.items() if k != "path"}
        record["path"] = str(project["path"].relative_to(ROOT)).replace("\\", "/")
        records.append(record)
    PROJECT_INDEX.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def load_project_index() -> None:
    if not PROJECT_INDEX.exists():
        return
    try:
        records = json.loads(PROJECT_INDEX.read_text(encoding="utf-8"))
        for record in records:
            path = safe_path(ROOT, record.pop("path"))
            if path.exists() and path.is_dir():
                PROJECTS[record["id"]] = {**record, "path": path}
    except (ValueError, KeyError, json.JSONDecodeError):
        return


class ToolExecutor:
    """Local tools with explicit boundaries and concise, serializable output."""

    ALLOWED_PREFIXES = ("python", "pytest", "npm", "node")

    def __init__(self, workspace: Path, cancel_check: Any | None = None):
        self.workspace = workspace
        self.cancel_check = cancel_check or (lambda: False)

    def list_files(self, path: str = ".") -> dict[str, Any]:
        root = safe_path(self.workspace, path)
        files = []
        for item in sorted(root.rglob("*")):
            relative = item.relative_to(self.workspace)
            if item.is_file() and not any(part in IGNORED_DIRS for part in relative.parts):
                files.append(str(item.relative_to(self.workspace)).replace("\\", "/"))
        return {"files": files}

    def read_file(self, path: str) -> dict[str, Any]:
        target = safe_path(self.workspace, path)
        text = target.read_text(encoding="utf-8")
        return {"path": path, "content": text, "lines": len(text.splitlines())}

    def write_file(self, path: str, content: str) -> dict[str, Any]:
        target = safe_path(self.workspace, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        before = target.read_text(encoding="utf-8") if target.exists() else ""
        target.write_text(content, encoding="utf-8")
        return self._change_result(path, before, content)

    def replace_text(self, path: str, old: str, new: str) -> dict[str, Any]:
        """Apply one exact replacement so the patch remains small and auditable."""
        target = safe_path(self.workspace, path)
        before = target.read_text(encoding="utf-8")
        occurrences = before.count(old)
        if occurrences != 1:
            raise ValueError(f"expected exactly one match, found {occurrences}")
        after = before.replace(old, new, 1)
        target.write_text(after, encoding="utf-8")
        result = self._change_result(path, before, after)
        result["operation"] = "exact_replace"
        return result

    @staticmethod
    def _change_result(path: str, before: str, after: str) -> dict[str, Any]:
        diff = "".join(difflib.unified_diff(
            before.splitlines(True), after.splitlines(True),
            fromfile=f"a/{path}", tofile=f"b/{path}"
        ))
        return {"path": path, "changed": before != after, "diff": diff,
                "added_lines": sum(1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")),
                "removed_lines": sum(1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---"))}

    def run_command(self, command: str) -> dict[str, Any]:
        try:
            parts = shlex.split(command)
        except ValueError as exc:
            raise ValueError(f"invalid command syntax: {exc}") from exc
        if not parts or parts[0].lower() not in self.ALLOWED_PREFIXES:
            raise ValueError("command is outside the safe allowlist")
        if any(token in command for token in ("&&", "||", ";", "|", ">", "<")):
            raise ValueError("shell chaining and redirection are disabled")
        if any(token in parts for token in ("-c", "-e", "--eval", "--exec")):
            raise ValueError("inline code execution is disabled")
        for token in parts[1:]:
            if os.path.isabs(token) or (len(token) > 1 and token[1] == ":"):
                raise ValueError("absolute command paths are disabled")
        proc = subprocess.Popen(
            parts, cwd=self.workspace, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, shell=False
        )
        deadline = time.monotonic() + 20
        while proc.poll() is None:
            if self.cancel_check():
                proc.kill()
                stdout, stderr = proc.communicate()
                output = (stdout + stderr).strip()
                return {"command": command, "returncode": -1, "output": output[-6000:],
                        "ok": False, "cancelled": True}
            if time.monotonic() >= deadline:
                proc.kill()
                stdout, stderr = proc.communicate()
                output = (stdout + stderr).strip()
                return {"command": command, "returncode": -1, "output": output[-6000:],
                        "ok": False, "timed_out": True}
            time.sleep(0.05)
        stdout, stderr = proc.communicate()
        output = (stdout + stderr).strip()
        return {"command": command, "returncode": proc.returncode,
                "output": output[-6000:], "ok": proc.returncode == 0}

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        methods = {"list_files": self.list_files, "read_file": self.read_file,
                   "write_file": self.write_file, "replace_text": self.replace_text,
                   "run_command": self.run_command}
        if name not in methods:
            raise ValueError(f"unknown tool: {name}")
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be an object")
        return methods[name](**arguments)


@dataclass(frozen=True)
class ToolSpec:
    """A small, inspectable tool contract used by both the model and executor."""

    name: str
    description: str
    parameters: dict[str, str]
    mutates_workspace: bool = False
    parallel_safe: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "parameters": dict(self.parameters),
                "mutates_workspace": self.mutates_workspace,
                "parallel_safe": self.parallel_safe}

    def model_schema(self) -> dict[str, Any]:
        properties = {name: {"type": "string"} for name in self.parameters}
        return {"type": "object", "properties": properties,
                "required": list(self.parameters), "additionalProperties": False}


class ToolRegistry:
    """Central registry that standardizes tool discovery and error results."""

    def __init__(self, executor: ToolExecutor):
        self.executor = executor
        self._handlers = {
            "list_files": executor.list_files,
            "read_file": executor.read_file,
            "write_file": executor.write_file,
            "replace_text": executor.replace_text,
            "run_command": executor.run_command,
        }
        self._specs = {spec.name: spec for spec in TOOL_SPECS}
        self._execution_lock = threading.Lock()

    def schemas(self) -> list[dict[str, Any]]:
        return self.schemas_for(self._specs.values())

    @staticmethod
    def schemas_for(specs: Any) -> list[dict[str, Any]]:
        return [{"type": "function", "function": {
            "name": spec.name, "description": spec.description,
            "parameters": spec.model_schema()}}
            for spec in specs]

    def specs(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        spec = self.get(name)
        if spec is None or name not in self._handlers:
            return {"ok": False, "tool": name, "error": f"unknown tool: {name}"}
        if not isinstance(arguments, dict):
            return {"ok": False, "tool": name, "error": "tool arguments must be an object"}
        missing = [key for key in spec.parameters if key not in arguments]
        unknown = [key for key in arguments if key not in spec.parameters]
        if missing:
            return {"ok": False, "tool": name,
                    "error": "missing required arguments: " + ", ".join(missing)}
        if unknown:
            return {"ok": False, "tool": name,
                    "error": "unknown arguments: " + ", ".join(unknown)}
        try:
            if spec.parallel_safe:
                result = self._handlers[name](**arguments)
            else:
                with self._execution_lock:
                    result = self._handlers[name](**arguments)
            return {"ok": True, "tool": name, **result}
        except Exception as exc:
            return {"ok": False, "tool": name, "error": str(exc)}


TOOL_SPECS = [
    ToolSpec("list_files", "List files in the isolated task workspace.", {"path": "string"}),
    ToolSpec("read_file", "Read a UTF-8 text file.", {"path": "string"}),
    ToolSpec("write_file", "Create or replace a UTF-8 text file.",
             {"path": "string", "content": "string"}, mutates_workspace=True, parallel_safe=False),
    ToolSpec("replace_text", "Replace exactly one matching text span and return a minimal diff.",
             {"path": "string", "old": "string", "new": "string"}, mutates_workspace=True, parallel_safe=False),
    ToolSpec("run_command", "Run one allowlisted local command.",
             {"command": "string"}, parallel_safe=False),
]

# Compatibility view used by the health endpoint and existing callers.
TOOLS = [spec.as_dict() for spec in TOOL_SPECS]


def run_snapshot(run: dict[str, Any]) -> dict[str, Any]:
    """Return the serializable state needed to restore a completed Run."""
    return {
        "schema_version": RUN_SCHEMA_VERSION,
        "run_id": run["id"],
        "task": run["task"],
        "mode": run["mode"],
        "approval_mode": run.get("approval_mode", "auto"),
        "pending_approval": run.get("pending_approval"),
        "cancel_requested": run.get("cancel_requested", False),
        "tool_call_count": run.get("tool_call_count", 0),
        "task_type": run.get("task_type", run.get("contract", {}).get("task_type", "change")),
        "project": run.get("project", {}),
        "state": run["state"],
        "summary": run.get("summary", ""),
        "finished": run.get("finished", False),
        "created_at": run.get("created_at", ""),
        "updated_at": run.get("updated_at", ""),
        "events": run.get("events", []),
        "ledger": run.get("ledger", []),
        "diffs": run.get("diffs", []),
        "contract": run.get("contract", {}),
        "evidence": run.get("evidence", {}),
        "evidence_details": run.get("evidence_details", {}),
        "trust_score": run.get("trust_score", 0),
        "boundary_violations": run.get("boundary_violations", 0),
        "last_event_id": run.get("last_event_id"),
    }


def persist_run(run: dict[str, Any]) -> None:
    """Atomically persist a Run without exposing locks or filesystem paths."""
    state_path = run.get("state_path")
    if not state_path:
        return
    persist_lock = run.setdefault("persist_lock", threading.Lock())
    with persist_lock:
        with run["lock"]:
            run["updated_at"] = timestamp()
            snapshot = run_snapshot(run)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = state_path.with_name(
            f".{state_path.name}.{threading.get_ident()}.tmp"
        )
        temporary.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        for attempt in range(4):
            try:
                os.replace(temporary, state_path)
                break
            except PermissionError:
                if attempt == 3:
                    raise
                time.sleep(0.02 * (2 ** attempt))


def load_run_history() -> None:
    """Restore persisted Runs so Trace and replay survive a service restart."""
    runs_root = ROOT / ".runs"
    if not runs_root.exists():
        return
    for state_path in sorted(runs_root.glob("*/run.json")):
        try:
            snapshot = json.loads(state_path.read_text(encoding="utf-8"))
            run_id = snapshot["run_id"]
            run_dir = state_path.parent
            workspace = run_dir / "workspace"
            if not workspace.is_dir():
                workspace = run_dir
            RUNS[run_id] = {
                "id": run_id,
                "task": snapshot.get("task", ""),
                "mode": snapshot.get("mode", "mock"),
                "approval_mode": snapshot.get("approval_mode", "auto"),
                "pending_approval": snapshot.get("pending_approval"),
                "cancel_requested": snapshot.get("cancel_requested", False),
                "tool_call_count": snapshot.get("tool_call_count", 0),
                "task_type": snapshot.get("task_type", snapshot.get("contract", {}).get("task_type", "change")),
                "workspace": workspace,
                "events": snapshot.get("events", []),
                "ledger": snapshot.get("ledger", []),
                "diffs": snapshot.get("diffs", []),
                "state": snapshot.get("state", "FAILED"),
                "summary": snapshot.get("summary", ""),
                "finished": snapshot.get("finished", True),
                "created_at": snapshot.get("created_at", ""),
                "updated_at": snapshot.get("updated_at", ""),
                "lock": threading.Lock(),
                "ledger_path": run_dir / "evidence.ndjson",
                "state_path": state_path,
                "contract": snapshot.get("contract", {}),
                "project": snapshot.get("project", {}),
                "evidence": snapshot.get("evidence", {}),
                "evidence_details": snapshot.get("evidence_details", {}),
                "trust_score": snapshot.get("trust_score", 0),
                "boundary_violations": snapshot.get("boundary_violations", 0),
                "last_event_id": snapshot.get("last_event_id"),
                "approval_condition": threading.Condition(threading.Lock()),
                "persist_lock": threading.Lock(),
            }
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            continue


def emit(run: dict[str, Any], kind: str, title: str, detail: str = "",
         tool: str | None = None, payload: Any = None, status: str = "active",
         phase: str | None = None, action: str | None = None,
         evidence_type: str | None = None, verification_status: str = "pending",
         affected_files: list[str] | None = None,
         parent_event_id: int | None = None, input_value: Any = None,
         output_value: Any = None) -> int:
    """Append a human-readable event and a causal, durable ledger record."""
    event = {
        "id": len(run["events"]) + 1,
        "run_id": run["id"],
        "time": now(),
        "timestamp": timestamp(),
        "kind": kind,
        "title": title,
        "detail": detail,
        "tool": tool,
        "action": action or title,
        "phase": phase,
        "evidence_type": evidence_type,
        "verification_status": verification_status,
        "affected_files": affected_files or [],
        "parent_event_id": parent_event_id,
        "input": input_value,
        "output": output_value,
        "payload": payload,
        "status": status,
    }
    with run["lock"]:
        run["events"].append(event)
        run["ledger"].append({k: event[k] for k in
                              ("id", "run_id", "timestamp", "kind", "title", "tool",
                               "action", "phase", "evidence_type", "verification_status",
                               "affected_files", "parent_event_id", "status")})
        with run["ledger_path"].open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    persist_run(run)
    return event["id"]


def compact(value: Any, limit: int = 420) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"


def _message_chars(messages: list[dict[str, Any]]) -> int:
    return sum(len(str(message.get("content", ""))) for message in messages)


def _compact_message_content(content: Any, limit: int = 420) -> str:
    """Keep verification facts while dropping bulky source/output text."""
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    try:
        parsed = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        parsed = None
    if isinstance(parsed, dict):
        keys = ("ok", "tool", "phase", "command", "returncode", "timed_out",
                "error", "path", "changed", "added_lines", "removed_lines")
        summary = {key: parsed[key] for key in keys if key in parsed}
        if isinstance(parsed.get("output"), str):
            summary["output_tail"] = parsed["output"][-160:]
        if isinstance(parsed.get("content"), str):
            summary["content_excerpt"] = compact(parsed["content"], 160)
        return "[compacted evidence] " + json.dumps(summary, ensure_ascii=False)
    if len(content) <= limit:
        return content
    head = max(80, limit // 2)
    tail = max(60, limit - head - 24)
    return content[:head] + " … [content compacted] … " + content[-tail:]


def apply_context_budget(messages: list[dict[str, Any]],
                         max_chars: int = MAX_CONTEXT_CHARS,
                         preserve_recent: int = 6) -> tuple[list[dict[str, Any]], dict[str, int | bool]]:
    """Compact old model/tool content without removing causal message structure."""
    before = _message_chars(messages)
    if before <= max_chars:
        return messages, {"compacted": False, "before_chars": before,
                          "after_chars": before, "messages_compacted": 0}
    result = [dict(message) for message in messages]
    cutoff = max(2, len(result) - preserve_recent)
    compacted_count = 0
    for index, message in enumerate(result):
        if index < 2 or index >= cutoff:
            continue
        if isinstance(message.get("content"), str):
            message["content"] = _compact_message_content(message["content"], 420)
            compacted_count += 1
    after = _message_chars(result)
    if after > max_chars:
        for index, message in enumerate(result):
            if index < 2 or not isinstance(message.get("content"), str):
                continue
            if message.get("role") == "tool" or index < cutoff:
                message["content"] = _compact_message_content(message["content"], 220)
                compacted_count += 1
        after = _message_chars(result)
    return result, {"compacted": True, "before_chars": before,
                    "after_chars": after, "messages_compacted": compacted_count}


class ModelAdapter:
    def __init__(self, mode: str):
        self.mode = mode

    def decide(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        if self.mode == "mock":
            return {"type": "finish", "summary": "演示序列由本地 Mock 模型完成"}
        base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        url = base + "/chat/completions"
        request_body = {"model": os.getenv("AURORA_MODEL", "gpt-4o-mini"),
                        "messages": messages,
                        "tools": ToolRegistry.schemas_for(TOOL_SPECS),
                        "tool_choice": "auto"}
        req = Request(url, data=json.dumps(request_body).encode(), method="POST",
                      headers={"Content-Type": "application/json",
                               "Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]})
        try:
            with urlopen(req, timeout=60) as response:
                data = json.loads(response.read().decode())
        except (HTTPError, URLError, KeyError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"model request failed: {exc}") from exc
        message = data["choices"][0]["message"]
        calls = message.get("tool_calls") or []
        if calls:
            call = calls[0]
            return {"type": "tool_call", "tool": call["function"]["name"],
                    "arguments": json.loads(call["function"]["arguments"]),
                    "reason": message.get("content") or "model selected the next tool",
                    "assistant_message": message, "call_id": call.get("id")}
        return {"type": "finish", "summary": message.get("content", "task finished")}


def mock_steps() -> list[dict[str, Any]]:
    """Deterministic fixture for the no-key demonstration path."""
    return [
        {"kind": "state", "title": "Task understood",
         "detail": "定位删除功能与边界验收条件。", "state": "UNDERSTAND"},
        {"kind": "tool", "tool": "list_files", "arguments": {"path": "."},
         "reason": "先建立项目地图。"},
        {"kind": "tool", "tool": "read_file", "arguments": {"path": "todo.py"},
         "reason": "读取业务逻辑。"},
        {"kind": "tool", "tool": "read_file", "arguments": {"path": "tests/test_todo.py"},
         "reason": "确认测试与验收标准。"},
        {"kind": "state", "title": "Acceptance contract locked",
         "detail": "删除最后一项、保留非法索引安全行为，并要求回归测试通过。", "state": "PLAN"},
        {"kind": "state", "title": "Execution started",
         "detail": "进入受控工具执行阶段。", "state": "EXECUTE"},
        {"kind": "tool", "tool": "run_command",
         "arguments": {"command": "python -m unittest discover -s tests -v"},
         "reason": "先复现当前故障，建立修改前证据。", "phase": "baseline"},
        {"kind": "tool", "tool": "replace_text",
         "arguments": {"path": "todo.py",
                       "old": "if 0 <= index < len(self.items) - 1:",
                       "new": "if 0 <= index < len(self.items):"},
         "reason": "使用单一精确替换，控制补丁影响面。", "phase": "patch"},
        {"kind": "tool", "tool": "run_command",
         "arguments": {"command": "python -m unittest discover -s tests -v"},
         "reason": "用真实测试验证修复。"},
        {"kind": "state", "title": "Verification gate passed",
         "detail": "修改前失败、最小补丁和修改后回归结果均已记录。", "state": "VERIFY"},
        {"kind": "finish", "summary": "已修复 Todo 删除边界 Bug，并通过全部 5 项单元测试。"},
    ]


def contract_for(task: str, project: dict[str, Any] | None = None,
                 task_type: str | None = None) -> dict[str, Any]:
    """Create a task-aware contract without allowing the model to weaken it."""
    resolved_type = infer_task_type(task, task_type)
    baseline_failure_required = resolved_type == "repair"
    checks = ["baseline_failure_captured", "minimal_patch_recorded",
              "regression_tests_passed", "workspace_boundary_respected"]
    baseline_label = "复现基线故障" if baseline_failure_required else "记录基线状态"
    baseline_policy = "observe_failure" if baseline_failure_required else "establish_green_baseline"
    return {
        "goal": task,
        "task_type": resolved_type,
        "task_type_label": {"repair": "Bug 修复", "feature": "功能新增",
                             "refactor": "结构重构", "change": "一般变更"}[resolved_type],
        "baseline_policy": baseline_policy,
        "checks": checks,
        "gate_definitions": {
            "baseline_failure_captured": {"weight": 25, "label": baseline_label,
                                           "policy": baseline_policy},
            "minimal_patch_recorded": {"weight": 25, "label": "记录最小补丁"},
            "regression_tests_passed": {"weight": 25, "label": "回归测试通过"},
            "workspace_boundary_respected": {"weight": 25, "label": "未越过工作区边界"},
        },
        "risk": "LOW · isolated workspace / allowlisted commands",
        "project": (project or {}).get("name", "Unknown project"),
    }


def phase_for_tool(run: dict[str, Any], tool: str, requested: str | None = None) -> str:
    if requested:
        return requested
    if tool in {"write_file", "replace_text"}:
        return "patch"
    if tool == "run_command":
        has_patch = bool(run.get("diffs"))
        has_baseline = any(
            isinstance(event.get("payload"), dict)
            and event.get("payload", {}).get("phase") == "baseline"
            for event in run.get("events", [])
        )
        if not has_baseline and not has_patch:
            return "baseline"
        if has_patch:
            return "regression"
    return "context"


def evidence_for_phase(phase: str | None, run: dict[str, Any] | None = None) -> str | None:
    if phase == "baseline":
        policy = (run or {}).get("contract", {}).get("baseline_policy", "observe_failure")
        return "baseline_failure" if policy == "observe_failure" else "baseline_status"
    return {"patch": "minimal_patch", "regression": "regression_test",
            "gate": "acceptance_gate"}.get(phase)


def update_evidence_score(run: dict[str, Any]) -> None:
    events = run["events"]
    baseline_events = [
        e for e in events
        if e.get("kind") in {"tool_result", "error"}
        and e.get("tool") == "run_command"
        and isinstance(e.get("payload"), dict)
        and e.get("payload", {}).get("phase") == "baseline"
    ]
    baseline_failure = any(
        e.get("payload", {}).get("ok") is False
        for e in baseline_events
    )
    baseline_green = any(
        e.get("payload", {}).get("ok") is True
        for e in baseline_events
    )
    baseline_policy = run.get("contract", {}).get("baseline_policy", "observe_failure")
    baseline = baseline_failure if baseline_policy == "observe_failure" else baseline_green
    patched = any(isinstance(diff, str) and bool(diff.strip()) for diff in run.get("diffs", []))
    verified = any(
        e.get("kind") == "tool_result"
        and e.get("tool") == "run_command"
        and isinstance(e.get("payload"), dict)
        and e.get("payload", {}).get("phase") == "regression"
        and e.get("payload", {}).get("ok") is True
        for e in events
    )
    boundary_ok = run.get("boundary_violations", 0) == 0
    run["evidence"] = {"baseline_failure_captured": baseline,
                       "minimal_patch_recorded": patched,
                       "regression_tests_passed": verified,
                       "workspace_boundary_respected": boundary_ok}
    reasons = {
        "baseline_failure_captured": (
            "已观察到修改前测试失败" if baseline_policy == "observe_failure"
            else "已确认修改前测试基线为绿色"
        ),
        "minimal_patch_recorded": "已生成非空 unified diff",
        "regression_tests_passed": "修改后回归命令返回成功",
        "workspace_boundary_respected": "没有文件或命令越过隔离工作区",
    }
    definitions = run.get("contract", {}).get("gate_definitions", {})
    run["evidence_details"] = {
        key: {"passed": value,
              "weight": definitions.get(key, {}).get("weight", 25),
              "reason": reasons[key]}
        for key, value in run["evidence"].items()
    }
    run["trust_score"] = sum(
        details["weight"] for details in run["evidence_details"].values()
        if details["passed"]
    )


def missing_evidence(run: dict[str, Any]) -> list[str]:
    update_evidence_score(run)
    definitions = run.get("contract", {}).get("gate_definitions", {})
    return [definitions.get(key, {}).get("label", key)
            for key, passed in run["evidence"].items() if not passed]


def complete_run(run: dict[str, Any], summary: str) -> bool:
    missing = missing_evidence(run)
    if missing:
        run["state"] = "VERIFY"
        emit(run, "guard", "Completion blocked",
             "尚未满足验收条件：" + "、".join(missing), phase="gate",
             evidence_type="acceptance_gate", verification_status="failed",
             status="failed", parent_event_id=run.get("last_event_id"))
        return False
    run["state"] = "VERIFY"
    gate_id = emit(run, "state", "Verification gate passed",
                   "四项验收条件均已满足，证据链闭合", phase="gate",
                   evidence_type="acceptance_gate", verification_status="passed",
                   status="done", parent_event_id=run.get("last_event_id"))
    run["last_event_id"] = gate_id
    run["state"] = "COMPLETED"
    run["summary"] = summary
    emit(run, "finish", "Task completed", summary, phase="complete",
         evidence_type="completion", verification_status="passed",
         status="done", parent_event_id=gate_id)
    return True


def _approval_condition(run: dict[str, Any]) -> threading.Condition:
    return run.setdefault("approval_condition", threading.Condition(threading.Lock()))


def is_cancel_requested(run: dict[str, Any]) -> bool:
    with _approval_condition(run):
        return bool(run.get("cancel_requested", False))


def ensure_run_active(run: dict[str, Any]) -> None:
    if is_cancel_requested(run):
        raise RunCancelled("run cancelled by user")


def request_cancel(run: dict[str, Any]) -> bool:
    condition = _approval_condition(run)
    with condition:
        if run.get("finished") or run.get("state") in TERMINAL_STATES:
            return False
        run["cancel_requested"] = True
        run["state"] = "CANCELLED"
        run["summary"] = "Run cancelled by user"
        run["pending_approval"] = None
        condition.notify_all()
    event_id = emit(run, "cancel", "Run cancellation requested",
                    "The current Agent step will stop cooperatively.", phase="lifecycle",
                    evidence_type="cancellation", verification_status="observed",
                    status="done", parent_event_id=run.get("last_event_id"))
    run["last_event_id"] = event_id
    persist_run(run)
    return True


def resolve_approval(run: dict[str, Any], decision: str) -> bool:
    if decision not in {"approve", "reject"}:
        raise ValueError("approval decision must be approve or reject")
    condition = _approval_condition(run)
    with condition:
        pending = run.get("pending_approval")
        if not pending:
            return False
        run["_approval_result"] = decision
        run["pending_approval"] = None
        if not run.get("cancel_requested"):
            run["state"] = run.pop("approval_resume_state", "EXECUTE")
        condition.notify_all()
    event_id = emit(run, "approval", "Tool approval granted" if decision == "approve" else "Tool approval rejected",
                    f"Human decision: {decision}.", tool=pending.get("tool"), phase=pending.get("phase"),
                    action="resolve_tool_approval", evidence_type="permission_decision",
                    verification_status="passed" if decision == "approve" else "failed",
                    status="done" if decision == "approve" else "failed",
                    affected_files=[pending["arguments"].get("path")] if isinstance(pending.get("arguments", {}).get("path"), str) else [],
                    parent_event_id=pending.get("event_id"), input_value=pending.get("arguments"),
                    output_value={"decision": decision})
    run["last_event_id"] = event_id
    persist_run(run)
    return True


def wait_for_approval(run: dict[str, Any], tool: str, arguments: dict[str, Any],
                      phase: str, parent_event_id: int | None = None) -> str:
    if run.get("approval_mode", "auto") == "auto":
        return "approved"
    ensure_run_active(run)
    condition = _approval_condition(run)
    with condition:
        run["approval_resume_state"] = run.get("state", "EXECUTE")
        run["pending_approval"] = {
            "tool": tool, "arguments": arguments, "phase": phase,
            "requested_at": timestamp(),
        }
        run["_approval_result"] = None
        run["state"] = "WAITING_APPROVAL"
    request_event_id = emit(
        run, "approval", "Approval required",
        "This operation is waiting for human approval before execution.", tool=tool,
        phase=phase, action="request_tool_approval", evidence_type="permission_request",
        verification_status="pending", status="active", parent_event_id=parent_event_id,
        affected_files=[arguments.get("path")] if isinstance(arguments.get("path"), str) else [],
        input_value=arguments,
    )
    with condition:
        if run.get("pending_approval") is not None:
            run["pending_approval"]["event_id"] = request_event_id
        run["last_event_id"] = request_event_id
    persist_run(run)
    with condition:
        while run.get("_approval_result") is None and not run.get("cancel_requested"):
            condition.wait(timeout=0.5)
        if run.get("cancel_requested"):
            return "cancelled"
        return run.pop("_approval_result", "reject")


def execute_agent_tool(run: dict[str, Any], registry: ToolRegistry, tool: str,
                       arguments: dict[str, Any], reason: str = "",
                       requested_phase: str | None = None) -> dict[str, Any]:
    """Run one tool through the registry and connect decision to result."""
    ensure_run_active(run)
    phase = phase_for_tool(run, tool, requested_phase)
    if phase == "baseline" and tool == "run_command":
        baseline_policy = run.get("contract", {}).get("baseline_policy", "observe_failure")
        baseline_detail = (
            "Run the pre-patch verification command to observe the failure before editing."
            if baseline_policy == "observe_failure"
            else "Run the pre-patch verification command to establish a green baseline before editing."
        )
        hypothesis_id = emit(
            run, "hypothesis", "Baseline hypothesis formed",
            baseline_detail,
            tool=tool, phase="baseline", action="form_baseline_hypothesis",
            evidence_type="baseline_hypothesis", verification_status="pending",
            parent_event_id=run.get("last_event_id"), status="active"
        )
        run["last_event_id"] = hypothesis_id
    decision_payload = dict(arguments)
    decision_payload["phase"] = phase
    decision_id = emit(
        run, "decision", f"Selected {tool}", reason, tool=tool,
        payload=decision_payload, phase=phase, action=f"select_{tool}",
        evidence_type=evidence_for_phase(phase, run), parent_event_id=run.get("last_event_id"),
        status="active", input_value=arguments
    )
    run["tool_call_count"] = run.get("tool_call_count", 0) + 1
    requires_approval = tool in {"write_file", "replace_text", "run_command"}
    approval = (wait_for_approval(run, tool, arguments, phase, decision_id)
                if requires_approval else "approved")
    if approval == "cancelled":
        raise RunCancelled("run cancelled while waiting for tool approval")
    if approval == "reject":
        result = {"ok": False, "tool": tool,
                  "error": "operation rejected by human approval",
                  "approval_rejected": True}
    else:
        ensure_run_active(run)
        result = registry.execute(tool, arguments)
    result["phase"] = phase
    if result.get("diff"):
        run["diffs"].append(result["diff"])
    if not result.get("ok") and "escapes the isolated workspace" in result.get("error", ""):
        run["boundary_violations"] = run.get("boundary_violations", 0) + 1
    affected_files = []
    if isinstance(result.get("path"), str):
        affected_files.append(result["path"])
    elif isinstance(arguments.get("path"), str):
        affected_files.append(arguments["path"])
    result_id = emit(
        run, "tool_result" if result.get("ok") else "error",
        f"{tool} returned" if result.get("ok") else f"{tool} blocked",
        compact(result.get("output") or result.get("content") or result),
        tool=tool, payload=result, phase=phase, action=f"execute_{tool}",
        evidence_type=evidence_for_phase(phase, run),
        verification_status="passed" if result.get("ok") else "failed",
        affected_files=affected_files, parent_event_id=decision_id,
        status="done" if result.get("ok") else "failed",
        input_value=arguments, output_value=result
    )
    run["last_event_id"] = result_id
    update_evidence_score(run)
    if result.get("cancelled"):
        raise RunCancelled("run cancelled while command was running")
    return result


def run_agent(run: dict[str, Any], mode: str):
    """Execute a run through one observable, gate-controlled path."""
    try:
        registry = ToolRegistry(
            ToolExecutor(run["workspace"], lambda: is_cancel_requested(run))
        )
        ensure_run_active(run)
        run["state"] = "UNDERSTAND"
        task_event_id = emit(
            run, "task", "Task received", run["task"],
            phase="understand", action="accept_task", evidence_type="task_input",
            verification_status="observed", status="done"
        )
        run["last_event_id"] = emit(
            run, "system", "Run initialized",
            "Isolated workspace created; Evidence Ledger is active.",
            phase="understand", evidence_type="run_initialized",
            verification_status="observed", status="done",
            parent_event_id=task_event_id
        )
        update_evidence_score(run)

        if mode == "mock":
            completed = False
            for step in mock_steps():
                ensure_run_active(run)
                for _ in range(8):
                    time.sleep(0.06)
                    ensure_run_active(run)
                if step["kind"] == "state":
                    run["state"] = step["state"]
                    phase = {
                        "UNDERSTAND": "understand", "PLAN": "plan",
                        "EXECUTE": "execute", "VERIFY": "verify",
                    }.get(step["state"], "lifecycle")
                    run["last_event_id"] = emit(
                        run, "state", step["title"], step["detail"],
                        phase=phase, evidence_type="state_transition",
                        verification_status="observed", status="done",
                        parent_event_id=run.get("last_event_id")
                    )
                elif step["kind"] == "finish":
                    if complete_run(run, step["summary"]):
                        completed = True
                        break
                else:
                    execute_agent_tool(
                        run, registry, step["tool"], step["arguments"],
                        reason=step["reason"], requested_phase=step.get("phase")
                    )
            if not completed:
                raise RuntimeError(
                    "the deterministic Mock fixture could not satisfy the selected acceptance contract"
                )
        else:
            contract = run["contract"]
            messages = [{"role": "system", "content": (
                "You are AURORA TRACE, an evidence-first local coding agent. "
                "Work only through the provided tools. First inspect the repository, "
                "follow the task-specific acceptance contract, make the smallest safe patch, "
                "run regression verification, and only then finish. "
                "Return one tool call at a time. Never claim success without evidence.\n"
                f"Task type: {contract['task_type']} ({contract['task_type_label']}). "
                f"Baseline policy: {contract['baseline_policy']}."
            )}, {"role": "user", "content": (
                f"Project profile: {json.dumps(run['project'].get('profile', {}), ensure_ascii=False)}\n"
                f"Task: {run['task']}\n"
                f"Acceptance gates: {json.dumps(contract['checks'], ensure_ascii=False)}"
            )}]
            adapter = ModelAdapter("live")
            for _ in range(12):
                ensure_run_active(run)
                messages, context_stats = apply_context_budget(messages)
                if context_stats["compacted"]:
                    run["last_event_id"] = emit(
                        run, "context", "Evidence context compacted",
                        f"Reduced model context from {context_stats['before_chars']} to "
                        f"{context_stats['after_chars']} characters while preserving "
                        "tool/result structure and verification facts.",
                        phase="context", action="compact_context",
                        evidence_type="context_compaction",
                        verification_status="observed", status="done",
                        parent_event_id=run.get("last_event_id"),
                        input_value={"chars": context_stats["before_chars"]},
                        output_value={"chars": context_stats["after_chars"],
                                      "messages_compacted": context_stats["messages_compacted"]}
                    )
                decision = adapter.decide(messages)
                if decision["type"] == "finish":
                    if complete_run(run, decision["summary"]):
                        break
                    missing = ", ".join(missing_evidence(run))
                    messages.append({
                        "role": "user",
                        "content": "Completion was blocked by the acceptance gate. "
                                   "Collect the missing evidence before finishing: " + missing,
                    })
                    continue
                tool, args = decision["tool"], decision["arguments"]
                phase = phase_for_tool(run, tool)
                run["state"] = {
                    "context": "UNDERSTAND", "baseline": "VERIFY",
                    "patch": "EXECUTE", "regression": "VERIFY",
                }.get(phase, run["state"])
                result = execute_agent_tool(
                    run, registry, tool, args,
                    reason=decision.get("reason", ""), requested_phase=phase
                )
                assistant_message = decision.get("assistant_message") or {
                    "role": "assistant", "content": decision.get("reason", "")
                }
                messages.extend([
                    assistant_message,
                    {"role": "tool", "tool_call_id": decision.get("call_id", ""),
                     "content": json.dumps(result, ensure_ascii=False)},
                ])
            else:
                raise RuntimeError("maximum iterations reached before the acceptance gate passed")
    except RunCancelled:
        run["state"] = "CANCELLED"
        run["summary"] = "Run cancelled by user"
    except Exception as exc:
        run["state"] = "FAILED"
        run["summary"] = str(exc)
        run["last_event_id"] = emit(
            run, "error", "Run stopped", str(exc), phase="lifecycle",
            evidence_type="failure", verification_status="failed", status="failed",
            parent_event_id=run.get("last_event_id")
        )
    finally:
        update_evidence_score(run)
        run["finished"] = True
        persist_run(run)


def start_run(task: str, mode: str, project_id: str = "demo",
              task_type: str | None = None,
              approval_mode: str = "auto") -> dict[str, Any]:
    if approval_mode not in APPROVAL_MODES:
        raise ValueError("approval_mode must be auto or manual")
    with PROJECT_LOCK:
        project = PROJECTS.get(project_id)
    if not project:
        raise ValueError("selected project does not exist")
    if project_id != "demo" and mode != "live":
        raise ValueError("uploaded projects require LIVE / MODEL API mode")
    contract = contract_for(task, project, task_type)
    if mode == "mock" and contract["task_type"] != "repair":
        raise ValueError(
            "内置 Mock Demo 只提供 Bug 修复夹具；请选择“Bug 修复”或切换到 Live Model。"
        )
    if mode == "live" and not os.getenv("OPENAI_API_KEY"):
        raise ValueError("Live 模式需要 OPENAI_API_KEY；请先在启动服务的终端中设置环境变量")
    run_id = uuid.uuid4().hex[:8]
    run_dir = ROOT / ".runs" / run_id
    workspace = run_dir / "workspace"
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(project["path"], workspace)
    ledger_path = run_dir / "evidence.ndjson"
    state_path = run_dir / "run.json"
    created_at = timestamp()
    run = {"id": run_id, "task": task, "mode": mode, "task_type": contract["task_type"],
           "approval_mode": approval_mode, "pending_approval": None,
           "cancel_requested": False, "tool_call_count": 0,
           "workspace": workspace,
           "events": [], "ledger": [], "diffs": [], "state": "QUEUED",
           "summary": "", "finished": False, "lock": threading.Lock(),
           "ledger_path": ledger_path, "contract": contract,
           "project": {k: v for k, v in project.items() if k != "path"},
           "evidence": {}, "evidence_details": {}, "trust_score": 0,
           "boundary_violations": 0, "last_event_id": None,
           "created_at": created_at, "updated_at": created_at,
           "state_path": state_path,
           "approval_condition": threading.Condition(threading.Lock()),
           "persist_lock": threading.Lock()}
    with RUN_LOCK: RUNS[run_id] = run
    persist_run(run)
    threading.Thread(target=run_agent, args=(run, mode), daemon=True).start()
    return {"run_id": run_id}


def multipart_file(content_type: str, body: bytes) -> tuple[str, bytes]:
    message = BytesParser(policy=email_default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
    )
    if not message.is_multipart():
        raise ValueError("expected multipart form upload")
    for part in message.iter_parts():
        filename = part.get_filename()
        if filename:
            return Path(filename).name, part.get_payload(decode=True) or b""
    raise ValueError("no project ZIP was attached")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        return

    def send_json(self, value: Any, code: int = 200):
        raw = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            raw = (WEB / "console.html").read_bytes(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers(); self.wfile.write(raw); return
        if self.path.startswith("/static/"):
            try:
                file = safe_web_path(self.path.removeprefix("/static/"))
            except ValueError:
                self.send_json({"error": "invalid static path"}, 400); return
            if file.exists() and file.is_file():
                raw = file.read_bytes(); self.send_response(200); self.send_header("Content-Type", ("text/css" if file.suffix == ".css" else "application/javascript") + "; charset=utf-8"); self.end_headers(); self.wfile.write(raw); return
        if self.path.startswith("/api/run/") and self.path.endswith("/export"):
            run_id = self.path.split("/")[-2]; run = RUNS.get(run_id)
            if not run: self.send_json({"error": "run not found"}, 404); return
            with run["lock"]:
                raw = json.dumps(run_snapshot(run), ensure_ascii=False, indent=2).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", f"attachment; filename=aurora-trace-{run_id}.json")
            self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw); return
        if self.path == "/api/runs":
            records = []
            with RUN_LOCK:
                history = list(RUNS.values())
            for run in history:
                with run["lock"]:
                    records.append({
                        "run_id": run["id"], "state": run["state"],
                        "task": run["task"], "mode": run["mode"],
                        "approval_mode": run.get("approval_mode", "auto"),
                        "pending_approval": run.get("pending_approval"),
                        "cancel_requested": run.get("cancel_requested", False),
                        "task_type": run.get("task_type", "change"),
                        "created_at": run.get("created_at", ""),
                        "updated_at": run.get("updated_at", ""),
                        "trust_score": run.get("trust_score", 0),
                        "summary": run.get("summary", ""),
                        "finished": run.get("finished", False),
                    })
            records.sort(key=lambda record: record["updated_at"], reverse=True)
            self.send_json({"runs": records}); return
        if self.path.startswith("/api/run/"):
            run_id = self.path.split("/")[-1]; run = RUNS.get(run_id)
            if not run: self.send_json({"error": "run not found"}, 404); return
            with run["lock"]:
                self.send_json({"id": run["id"], "state": run["state"],
                                "approval_mode": run.get("approval_mode", "auto"),
                                "pending_approval": run.get("pending_approval"),
                                "cancel_requested": run.get("cancel_requested", False),
                                "tool_call_count": run.get("tool_call_count", 0),
                                "task_type": run.get("task_type", "change"),
                                "events": run["events"],
                                "ledger": run["ledger"], "diffs": run["diffs"],
                                "summary": run["summary"], "finished": run["finished"],
                                "contract": run["contract"], "evidence": run["evidence"],
                                "evidence_details": run.get("evidence_details", {}),
                                "trust_score": run["trust_score"],
                                "created_at": run.get("created_at", ""),
                                "updated_at": run.get("updated_at", ""),
                                "project": run.get("project", {}),
                                "replay": {"event_count": len(run["events"]),
                                            "ledger_persisted": run["ledger_path"].exists(),
                                            "workspace": f".runs/{run['id']}/workspace"}})
            return
        if self.path == "/api/projects":
            with PROJECT_LOCK:
                projects = []
                for project in PROJECTS.values():
                    if "profile" not in project:
                        project["profile"] = profile_project(project["path"])
                    projects.append({k: v for k, v in project.items() if k != "path"})
            self.send_json({"projects": projects}); return
        if self.path == "/api/health":
            self.send_json({"service": "AURORA TRACE", "status": "online",
                            "mode": os.getenv("AURORA_MODE", "mock"),
                            "tools": [tool["name"] for tool in TOOLS]})
            return
        self.send_json({"service": "AURORA TRACE", "status": "online"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        if self.path == "/api/projects/import":
            if length > MAX_UPLOAD_BYTES + 1024 * 1024:
                self.send_json({"error": "upload exceeds the 10 MB limit"}, 413); return
            try:
                filename, data = multipart_file(self.headers.get("Content-Type", ""), self.rfile.read(length))
                self.send_json({"project": import_zip_project(filename, data)}, 201)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 400)
            return
        if self.path.startswith("/api/run/") and self.path.endswith("/approve"):
            run_id = self.path.split("/")[-2]
            run = RUNS.get(run_id)
            if not run:
                self.send_json({"error": "run not found"}, 404)
                return
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                decision = body.get("decision", "")
                if not resolve_approval(run, decision):
                    self.send_json({"error": "no approval is pending for this run"}, 409)
                    return
            except json.JSONDecodeError:
                self.send_json({"error": "request body must be valid JSON"}, 400)
                return
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 400)
                return
            with run["lock"]:
                self.send_json({"id": run["id"], "state": run["state"],
                                "approval_mode": run.get("approval_mode", "auto"),
                                "pending_approval": run.get("pending_approval"),
                                "cancel_requested": run.get("cancel_requested", False),
                                "tool_call_count": run.get("tool_call_count", 0),
                                "task_type": run.get("task_type", "change"),
                                "events": run["events"], "ledger": run["ledger"],
                                "diffs": run["diffs"], "summary": run["summary"],
                                "finished": run["finished"], "contract": run["contract"],
                                "evidence": run["evidence"],
                                "evidence_details": run.get("evidence_details", {}),
                                "trust_score": run["trust_score"],
                                "created_at": run.get("created_at", ""),
                                "updated_at": run.get("updated_at", ""),
                                "project": run.get("project", {})})
            return
        if self.path.startswith("/api/run/") and self.path.endswith("/cancel"):
            run_id = self.path.split("/")[-2]
            run = RUNS.get(run_id)
            if not run:
                self.send_json({"error": "run not found"}, 404)
                return
            if not request_cancel(run):
                self.send_json({"error": "run is already finished"}, 409)
                return
            with run["lock"]:
                self.send_json({"id": run["id"], "state": run["state"],
                                "approval_mode": run.get("approval_mode", "auto"),
                                "pending_approval": run.get("pending_approval"),
                                "cancel_requested": run.get("cancel_requested", False),
                                "tool_call_count": run.get("tool_call_count", 0),
                                "task_type": run.get("task_type", "change"),
                                "events": run["events"], "ledger": run["ledger"],
                                "diffs": run["diffs"], "summary": run["summary"],
                                "finished": run["finished"], "contract": run["contract"],
                                "evidence": run["evidence"],
                                "evidence_details": run.get("evidence_details", {}),
                                "trust_score": run["trust_score"],
                                "created_at": run.get("created_at", ""),
                                "updated_at": run.get("updated_at", ""),
                                "project": run.get("project", {})})
            return
        if self.path != "/api/run": self.send_json({"error": "not found"}, 404); return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self.send_json({"error": "request body must be valid JSON"}, 400); return
        task = body.get("task", "修复 Todo 项目的删除边界 Bug，补充测试并运行测试。")
        mode = body.get("mode") or os.getenv("AURORA_MODE", "mock")
        task_type = body.get("task_type") or "auto"
        approval_mode = body.get("approval_mode") or "auto"
        if mode not in {"mock", "live"}:
            self.send_json({"error": "mode must be mock or live"}, 400); return
        if task_type != "auto" and task_type not in TASK_TYPES:
            self.send_json({"error": "task_type must be auto, repair, feature, refactor, or change"}, 400); return
        if approval_mode not in APPROVAL_MODES:
            self.send_json({"error": "approval_mode must be auto or manual"}, 400); return
        try:
            self.send_json(start_run(task, mode, body.get("project_id", "demo"),
                                     task_type, approval_mode))
        except ValueError as exc:
            self.send_json({"error": str(exc)}, 400)


if __name__ == "__main__":
    load_project_index()
    load_run_history()
    print(f"AURORA TRACE listening on http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()

"""SSH extension: let the LLM (and sub-agents) drive a remote Ubuntu shell.

Everything here is gated on **manual configuration** (host/user/password). With
no credentials the client reports "not configured" and the tools stay dark.

* :class:`SSHConfig` — connection settings + a free-text ``notes`` field that
  documents the machine (specs, network limits, open ports).
* :class:`SSHClient` — a persistent paramiko connection reused across commands;
  both the paramiko import and the socket work are injectable, so the whole
  thing is unit-testable without a real server.
* :class:`SSHAgentSession` / :class:`SSHAgentManager` — a background agent loop
  that autonomously runs shell commands toward a task, while the operator can
  watch its log, pause/resume, inject instructions/answers, update the goal, or
  stop it at any time.
"""
from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .json_utils import compact_json, parse_json_object

PIP_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
_MAX_OUT = 12000        # cap per-stream command output kept in memory
_MAX_STEP_LOG = 60      # rolling window of steps retained per session


class SSHError(RuntimeError):
    pass


@dataclass(slots=True)
class SSHConfig:
    host: str = ""
    port: int = 22
    user: str = ""
    password: str = ""
    notes: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.host and self.user and self.password)

    def target(self) -> str:
        return f"{self.user}@{self.host}:{self.port}"

    def public(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "notes": self.notes,
            "password_set": bool(self.password),
        }


def paramiko_installed() -> bool:
    return importlib.util.find_spec("paramiko") is not None


async def _run_pip(cmd: list[str], *, timeout: int,
                   runner: Callable[..., Awaitable[tuple[int, str]]] | None
                   ) -> tuple[int, str]:
    if runner is not None:
        return await runner(cmd, timeout=timeout)

    def _sync() -> tuple[int, str]:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return proc.returncode, ((proc.stdout or "")[-600:] + (proc.stderr or "")[-400:])
        except FileNotFoundError as error:
            return 127, f"not found: {error}"
        except subprocess.TimeoutExpired:
            return 124, "timed out"

    return await asyncio.to_thread(_sync)


async def ensure_paramiko(*, auto_install: bool = True,
                          logger: Callable[[str], Any] | None = None,
                          import_check: Callable[[], bool] | None = None,
                          runner: Callable[..., Awaitable[tuple[int, str]]] | None = None,
                          ) -> dict[str, Any]:
    """Detect (and optionally pip-install via the China mirror) paramiko."""
    log = logger or (lambda _m: None)
    check = import_check or paramiko_installed
    status: dict[str, Any] = {"paramiko": False, "steps": [], "error": ""}
    if check():
        status["paramiko"] = True
        return status
    if not auto_install:
        status["error"] = "paramiko 未安装，且未开启自动安装"
        return status
    log("SSH 扩展：paramiko 未安装，正在通过清华镜像安装（轻量，仅本包）……")
    code, tail = await _run_pip(
        [sys.executable, "-m", "pip", "install", "paramiko", "-i", PIP_MIRROR, "--quiet"],
        timeout=900, runner=runner)
    status["steps"].append(f"pip install paramiko -> exit {code}")
    if code != 0 or not check():
        status["error"] = f"paramiko 安装失败：{tail[-200:]}"
        return status
    status["paramiko"] = True
    log("SSH 扩展：paramiko 安装成功")
    return status


def _open_paramiko(cfg: SSHConfig, timeout: float) -> Any:
    import paramiko

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=cfg.host, port=int(cfg.port or 22), username=cfg.user,
        password=cfg.password, timeout=timeout, banner_timeout=timeout,
        auth_timeout=timeout, look_for_keys=False, allow_agent=False)
    return client


def _run_paramiko(client: Any, command: str, timeout: float) -> tuple[int, str, str]:
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout, get_pty=False)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    return code, out, err


class SSHClient:
    """Persistent SSH connection reused across one-shot commands.

    Each :meth:`exec` is an independent ``exec_command`` (a fresh shell — ``cd``
    / env / activated venvs do NOT persist between calls), so chain with ``&&``
    or use absolute paths. The underlying transport is reused and transparently
    reconnected once if it went stale.
    """

    def __init__(self, config_provider: Callable[[], SSHConfig], *,
                 connect_timeout: float = 15.0,
                 opener: Callable[[SSHConfig, float], Any] | None = None,
                 runner: Callable[[Any, str, float], tuple[int, str, str]] | None = None,
                 ) -> None:
        self._config_provider = config_provider
        self._connect_timeout = float(connect_timeout)
        self._opener = opener or _open_paramiko
        self._runner = runner or _run_paramiko
        self._client: Any = None
        self._lock = asyncio.Lock()

    def config(self) -> SSHConfig:
        try:
            cfg = self._config_provider()
        except Exception:
            return SSHConfig()
        return cfg if cfg is not None else SSHConfig()

    @property
    def configured(self) -> bool:
        return self.config().configured

    async def _ensure(self, cfg: SSHConfig) -> Any:
        if self._client is not None:
            return self._client
        async with self._lock:
            if self._client is None:
                if self._opener is _open_paramiko and not paramiko_installed():
                    raise SSHError(
                        "paramiko 未安装（SSH 启用后后台会自动安装，稍等重试）")
                try:
                    self._client = await asyncio.to_thread(
                        self._opener, cfg, self._connect_timeout)
                except SSHError:
                    raise
                except Exception as error:
                    raise SSHError(
                        f"连不上 {cfg.target()}：{str(error)[:180]}") from error
        return self._client

    async def exec(self, command: str, *, timeout: float = 60.0,
                   max_out: int | None = None) -> dict[str, Any]:
        cfg = self.config()
        if not cfg.configured:
            raise SSHError(
                "SSH 未配置：请在插件配置填 ssh_host / ssh_user / ssh_password")
        command = str(command or "").strip()
        if not command:
            raise SSHError("命令为空")
        client = await self._ensure(cfg)
        try:
            code, out, err = await asyncio.to_thread(
                self._runner, client, command, float(timeout))
        except Exception as first:
            await self._drop()                      # stale connection → retry once
            client = await self._ensure(cfg)
            try:
                code, out, err = await asyncio.to_thread(
                    self._runner, client, command, float(timeout))
            except Exception as second:
                raise SSHError(
                    f"命令执行失败：{str(second or first)[:180]}") from second
        # 默认 12k 上限防止大输出灌进上下文；拉文件（base64 整块回传）显式放宽
        cap = int(max_out) if max_out else _MAX_OUT
        return {
            "exit_code": code,
            "stdout": out[:cap],
            "stderr": err[:cap],
            "truncated": len(out) > cap or len(err) > cap,
        }

    async def check(self) -> dict[str, Any]:
        """Connectivity probe: identity + kernel + distro in one round-trip."""
        return await self.exec(
            "whoami; uname -a; (lsb_release -ds 2>/dev/null || cat /etc/os-release 2>/dev/null | head -1)",
            timeout=self._connect_timeout + 15)

    async def _drop(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await asyncio.to_thread(client.close)
            except Exception:
                pass

    async def close(self) -> None:
        await self._drop()


@dataclass
class SSHStep:
    index: int
    thought: str = ""
    command: str = ""
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    note: str = ""

    def brief(self, out_limit: int = 1200) -> dict[str, Any]:
        data: dict[str, Any] = {"step": self.index}
        if self.thought:
            data["thought"] = self.thought[:300]
        if self.command:
            data["command"] = self.command
        if self.exit_code is not None:
            data["exit_code"] = self.exit_code
        if self.stdout:
            data["stdout"] = self.stdout[:out_limit]
        if self.stderr:
            data["stderr"] = self.stderr[:out_limit]
        if self.note:
            data["note"] = self.note
        return data


class SSHAgentSession:
    """One background agent driving the remote shell toward a task.

    Lifecycle states: pending → running ↔ paused / waiting → done | stopped | error.
    Control methods below are safe to call from other coroutines on the same
    event loop (asyncio is single-threaded — no locks needed for the flags).
    """

    def __init__(self, session_id: str, task: str, *, ssh: SSHClient,
                 llm: Callable[[str, str, str], Awaitable[str]], provider: str,
                 notes: str, system_prompt: str, max_steps: int = 30,
                 cmd_timeout: float = 60.0, char_budget: int = 16000,
                 logger: Callable[[str], Any] | None = None) -> None:
        self.id = session_id
        self.task = str(task or "").strip()
        self.state = "pending"
        self.steps: list[SSHStep] = []
        self.question = ""
        self.conclusion = ""
        self.error = ""
        self.created_at = time.time()
        self.updated_at = self.created_at
        self._ssh = ssh
        self._llm = llm
        self._provider = provider
        self._notes = notes
        self._system_prompt = system_prompt
        self._max_steps = max(1, int(max_steps))
        self._cmd_timeout = float(cmd_timeout)
        self._char_budget = int(char_budget)
        self._log = logger or (lambda _m: None)
        self._inbox: list[str] = []
        self._count = 0
        self._pause = False
        self._stop = False
        self._gate = asyncio.Event()
        self._gate.set()
        self._task_obj: asyncio.Task[Any] | None = None

    @property
    def finished(self) -> bool:
        return self.state in ("done", "stopped", "error")

    def start(self) -> None:
        if self._task_obj is None:
            self.state = "running"
            self._task_obj = asyncio.ensure_future(self._run())

    def pause(self) -> bool:
        if self.finished:
            return False
        self._pause = True
        self._touch()
        return True

    def resume(self) -> bool:
        if self.finished:
            return False
        self._pause = False
        if self.state in ("paused", "waiting"):
            self.state = "running"
        self._gate.set()
        self._touch()
        return True

    def stop(self) -> bool:
        if self.finished:
            return False
        self._stop = True
        self._gate.set()
        self._touch()
        return True

    def send(self, text: str, *, is_update: bool = False) -> bool:
        text = str(text or "").strip()
        if not text:
            return False
        if is_update:
            self.task = text
            self._inbox.append(f"【操作者更新了任务目标】{text}")
        else:
            self._inbox.append(f"【操作者留言/答复】{text}")
        self._pause = False
        if self.state in ("paused", "waiting"):
            self.state = "running"
        self._gate.set()
        self._touch()
        return True

    def status(self, *, steps: int = 6) -> dict[str, Any]:
        recent = self.steps[-max(1, int(steps)):] if self.steps else []
        return {
            "session_id": self.id,
            "state": self.state,
            "task": self.task,
            "steps_done": self._count,
            "max_steps": self._max_steps,
            "question": self.question,
            "conclusion": self.conclusion,
            "error": self.error,
            "pending_messages": len(self._inbox),
            "recent_steps": [s.brief() for s in recent],
        }

    def _touch(self) -> None:
        self.updated_at = time.time()

    def _drain(self) -> list[str]:
        msgs = self._inbox[:]
        self._inbox.clear()
        return msgs

    def _trim(self) -> None:
        if len(self.steps) > _MAX_STEP_LOG:
            self.steps = self.steps[-_MAX_STEP_LOG:]

    def _build_prompt(self, injected: list[str]) -> str:
        payload: dict[str, Any] = {
            "task": self.task,
            "machine_notes": self._notes or "（未提供机器说明）",
            "steps_done": self._count,
            "steps_remaining": self._max_steps - self._count,
            "recent_steps": [s.brief(out_limit=1500) for s in self.steps[-8:]],
        }
        if injected:
            payload["operator_messages"] = injected
        return compact_json(payload, self._char_budget)

    def _parse(self, raw: str) -> dict[str, Any]:
        try:
            data = parse_json_object(str(raw or ""))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {"done": True, "conclusion": str(raw or "").strip()[:800]}

    async def _run(self) -> None:
        try:
            while self._count < self._max_steps and not self._stop:
                if self._pause:
                    self.state = "paused"
                    self._gate.clear()
                    await self._gate.wait()
                    if self._stop:
                        break
                    self.state = "running"
                self._count += 1
                injected = self._drain()
                try:
                    raw = await self._llm(
                        self._provider, self._build_prompt(injected),
                        self._system_prompt)
                except Exception as error:
                    self.error = f"子agent模型调用失败：{str(error)[:160]}"
                    self.state = "error"
                    return
                decision = self._parse(raw)
                thought = str(decision.get("thought", "") or "").strip()
                ask = str(decision.get("ask", "") or "").strip()
                command = str(decision.get("command", "") or "").strip()
                done = bool(decision.get("done"))
                conclusion = str(decision.get("conclusion", "") or "").strip()

                if ask and not command:
                    self.steps.append(SSHStep(self._count, thought=thought, note=f"❓向操作者提问：{ask}"))
                    self._trim()
                    self.question = ask
                    self.state = "waiting"
                    self._gate.clear()
                    await self._gate.wait()
                    self.question = ""
                    if self._stop:
                        break
                    self.state = "running"
                    continue
                if done or not command:
                    self.conclusion = conclusion or thought or "（子agent未给出结论）"
                    if thought or conclusion:
                        self.steps.append(SSHStep(self._count, thought=thought, note=f"✅收尾：{self.conclusion[:200]}"))
                        self._trim()
                    self.state = "done"
                    return

                step = SSHStep(self._count, thought=thought, command=command)
                try:
                    result = await self._ssh.exec(command, timeout=self._cmd_timeout)
                    step.exit_code = result.get("exit_code")
                    step.stdout = str(result.get("stdout", ""))
                    step.stderr = str(result.get("stderr", ""))
                    if result.get("truncated"):
                        step.note = "输出过长已截断"
                except SSHError as error:
                    step.note = f"执行失败：{error}"
                except Exception as error:
                    step.note = f"执行异常：{str(error)[:160]}"
                self.steps.append(step)
                self._trim()
                self._touch()
                self._log(f"[SSH-agent {self.id[:6]}] #{self._count} {command[:60]} -> {step.exit_code}")

            if self._stop:
                self.state = "stopped"
            elif not self.finished:
                self.conclusion = self.conclusion or "达到步数上限，任务未显式收尾"
                self.state = "done"
        except asyncio.CancelledError:
            self.state = "stopped"
            raise
        except Exception as error:
            self.error = f"{type(error).__name__}: {str(error)[:160]}"
            self.state = "error"
        finally:
            self._touch()


class SSHAgentManager:
    """Owns the live SSH agent sessions and the operator control surface."""

    def __init__(self, *, ssh: SSHClient,
                 llm: Callable[[str, str, str], Awaitable[str]],
                 provider_getter: Callable[[], str],
                 notes_getter: Callable[[], str],
                 system_prompt: str, max_steps: int = 30,
                 cmd_timeout: float = 60.0, char_budget: int = 16000,
                 max_sessions: int = 3,
                 logger: Callable[[str], Any] | None = None) -> None:
        self._ssh = ssh
        self._llm = llm
        self._provider_getter = provider_getter
        self._notes_getter = notes_getter
        self._system_prompt = system_prompt
        self._max_steps = max_steps
        self._cmd_timeout = cmd_timeout
        self._char_budget = char_budget
        self._max_sessions = max(1, int(max_sessions))
        self._log = logger or (lambda _m: None)
        self._sessions: dict[str, SSHAgentSession] = {}

    def active(self) -> list[SSHAgentSession]:
        return [s for s in self._sessions.values() if not s.finished]

    def get(self, session_id: str) -> SSHAgentSession | None:
        return self._sessions.get(str(session_id or "").strip())

    def _prune(self) -> None:
        done = [s for s in self._sessions.values() if s.finished]
        if len(done) > 12:
            for old in sorted(done, key=lambda s: s.updated_at)[:len(done) - 12]:
                self._sessions.pop(old.id, None)

    def dispatch(self, task: str) -> SSHAgentSession:
        task = str(task or "").strip()
        if not task:
            raise SSHError("需要 task 任务描述")
        if not self._ssh.configured:
            raise SSHError("SSH 未配置：请在插件配置填 ssh_host / ssh_user / ssh_password")
        if len(self.active()) >= self._max_sessions:
            raise SSHError(
                f"同时运行的 SSH 子agent已达上限（{self._max_sessions}），先结束一个再派新任务")
        provider = str(self._provider_getter() or "").strip()
        if not provider:
            raise SSHError("没有可用的子agent模型（配置 subagent_provider_id 或 reply_provider_id）")
        self._prune()
        session_id = uuid.uuid4().hex[:12]
        session = SSHAgentSession(
            session_id, task, ssh=self._ssh, llm=self._llm, provider=provider,
            notes=self._notes_getter(), system_prompt=self._system_prompt,
            max_steps=self._max_steps, cmd_timeout=self._cmd_timeout,
            char_budget=self._char_budget, logger=self._log)
        self._sessions[session_id] = session
        session.start()
        return session

    def status(self, session_id: str = "", *, steps: int = 6) -> dict[str, Any] | None:
        session_id = str(session_id or "").strip()
        if session_id:
            session = self.get(session_id)
            return session.status(steps=steps) if session is not None else None
        rows = sorted(self._sessions.values(), key=lambda s: s.created_at)
        return {"sessions": [s.status(steps=2) for s in rows[-10:]],
                "active": len(self.active())}

    def control(self, session_id: str, action: str, message: str = "") -> dict[str, Any]:
        session = self.get(session_id)
        if session is None:
            return {"ok": False, "error": "没有这个 SSH 子agent会话（先 ssh_agent_status 查 id）"}
        action = str(action or "").lower().strip()
        if action in ("status", ""):
            return {"ok": True, **session.status()}
        if action == "pause":
            return {"ok": session.pause(), "state": session.state,
                    "hint": "已请求暂停，子agent会在当前命令后停下"}
        if action == "resume":
            return {"ok": session.resume(), "state": session.state}
        if action == "stop":
            session.stop()
            return {"ok": True, "state": "stopping", "hint": "已请求结束该任务"}
        if action in ("update", "更新", "更新任务"):
            return {"ok": session.send(message, is_update=True), "state": session.state}
        if action in ("ask", "say", "message", "answer", "提问", "答复", "留言"):
            return {"ok": session.send(message), "state": session.state}
        return {"ok": False,
                "error": f"未知操作：{action}（可用 status/pause/resume/update/ask/stop）"}

    async def stop_all(self) -> None:
        for session in list(self._sessions.values()):
            session.stop()
        tasks = [s._task_obj for s in self._sessions.values()
                 if s._task_obj is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)





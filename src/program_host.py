"""本机 studio 宿主：单端口承载 designer 渲染窗口与 programmer 小程序。

路由（全部只在 127.0.0.1 上监听）：
- ``/d/<design_id>``       designer 渲染窗口——HTML/SVG 文本，**最多存活 60 秒**
- ``/p/<program_id>/...``  programmer 小程序——bot 自己写的 Flask 应用，按 TTL 存活，
  同一端口不同页面；数据由程序自己实时写进注入的 ``DATA_DIR``
- ``/``                    索引页：列出正在运行的程序与存活的窗口

宿主本身只用标准库（wsgiref + ThreadingMixIn），零新增依赖；只有子程序代码
才需要 flask——第一次运行程序时自动 pip 安装（走清华镜像，装不上给出说明）。
项目存档（designer 的绘图 / programmer 的程序）统一落 ``registry.json``，
带标题与简介，供插件写入长期记忆。
"""
from __future__ import annotations

import json
import re
import subprocess


_ALLOW_AUTO_INSTALL = True


def set_auto_install(enabled: bool) -> None:
    """是否允许运行时自动 pip 安装（主人授权项，市场审查要求显式可关）。

    关掉后不缺依赖照常跑；缺依赖就返回失败与安装提示，绝不擅自动网络安装。
    """
    global _ALLOW_AUTO_INSTALL
    _ALLOW_AUTO_INSTALL = bool(enabled)


def auto_install_allowed() -> bool:
    return _ALLOW_AUTO_INSTALL
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server
from socketserver import ThreadingMixIn

DESIGN_TTL_SECONDS = 60.0  # 用户规格：渲染窗口最多存在 1 分钟（硬上限）
_SWEEP_INTERVAL = 15.0
_SLUG_RE = re.compile(r"[^0-9a-z]+")  # id 只留 ASCII：URL 路径安全

FlaskState = tuple[bool, str]
_flask_state: FlaskState | None = None


def ensure_flask() -> FlaskState:
    """Import flask; on first miss pip-install it (child programs need it)."""
    global _flask_state
    if _flask_state is not None:
        return _flask_state
    try:
        import flask

        _flask_state = (True, str(getattr(flask, "__version__", "ok")))
        return _flask_state
    except Exception:
        pass
    if not _ALLOW_AUTO_INSTALL:
        _flask_state = (False, "缺少 flask，且 auto_install_deps 已关闭（可自行 pip install flask）")
        return _flask_state
    for index in ("https://pypi.tuna.tsinghua.edu.cn/simple", ""):
        command = [sys.executable, "-m", "pip", "install", "--quiet", "flask>=3.0"]
        if index:
            command += ["-i", index]
        try:
            subprocess.run(command, capture_output=True, timeout=300, check=True)
            import flask

            _flask_state = (True, str(getattr(flask, "__version__", "ok")))
            return _flask_state
        except Exception:
            continue
    _flask_state = (False, "flask 自动安装失败（检查服务器网络/ pip）")
    return _flask_state


def slugify(text: str, fallback: str) -> str:
    slug = _SLUG_RE.sub("-", str(text or "").strip().lower()).strip("-")[:24]
    return slug or fallback


class _ThreadedWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True
    allow_reuse_address = True


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, *args: Any) -> None:  # 静默：别刷 AstrBot 日志
        pass


class ProgramHost:
    """Owns the loopback server, running programs, live design windows and the
    project registry. All methods are sync — the plugin calls them via
    ``asyncio.to_thread`` where needed."""

    def __init__(
        self,
        root: str | Path,
        *,
        port: int = 8765,
        program_ttl_seconds: float = 600.0,
        logger: Callable[[str], Any] | None = None,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "programs").mkdir(exist_ok=True)
        self.registry_path = self.root / "registry.json"
        self.desired_port = int(port)
        self.program_ttl = max(60.0, float(program_ttl_seconds))
        self.log = logger or (lambda message: None)
        self._lock = threading.RLock()
        self.registry: list[dict[str, Any]] = self._load_registry()
        self._apps: dict[str, dict[str, Any]] = {}   # pid → {app, started_at, data_dir}
        self._designs: dict[str, dict[str, Any]] = {}  # did → {content, type, expires_at}
        self._server: Any = None
        self._thread: threading.Thread | None = None
        self.port: int | None = None
        self._closing = False

    # ------------------------------------------------------------ 服务
    def start(self) -> int:
        """Start the loopback server (idempotent); returns the actual port."""
        with self._lock:
            if self._server is not None:
                return int(self.port)
            last_error: Exception | None = None
            for candidate in range(self.desired_port, self.desired_port + 20):
                try:
                    server = make_server(
                        "127.0.0.1", candidate, self._dispatch,
                        server_class=_ThreadedWSGIServer, handler_class=_QuietHandler,
                    )
                    break
                except OSError as error:
                    last_error = error
                    server = None
            if server is None:
                raise RuntimeError(
                    f"studio 端口 {self.desired_port}~{self.desired_port + 19} 都被占用：{last_error}")
            self._server = server
            self.port = server.server_address[1]
            self._thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.5},
                name="longmem-studio", daemon=True,
            )
            self._thread.start()
            self.log(f"studio 宿主已启动：http://127.0.0.1:{self.port}/（programs {len(self._apps)}）")
            janitor = threading.Thread(
                target=self._janitor_loop, name="longmem-studio-janitor", daemon=True)
            janitor.start()
            return int(self.port)

    def close(self) -> None:
        self._closing = True
        with self._lock:
            self._apps.clear()
            self._designs.clear()
            server, self._server = self._server, None
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass

    def _janitor_loop(self) -> None:
        while not self._closing:
            time.sleep(_SWEEP_INTERVAL)
            try:
                self.sweep()
            except Exception as error:
                self.log(f"studio 清扫异常：{error}")

    # ------------------------------------------------------------ WSGI
    def _dispatch(self, environ: dict, start_response: Callable) -> list[bytes]:
        path = environ.get("PATH_INFO", "/")
        if path.startswith("/d/"):
            return self._serve_design(path[3:].strip("/"), start_response)
        if path.startswith("/p/"):
            pid, _, sub = path[3:].partition("/")
            return self._serve_program(pid.strip("/"), "/" + sub, environ, start_response)
        return self._serve_index(start_response)

    @staticmethod
    def _respond(start_response: Callable, status: str, body: str,
                 content_type: str = "text/html; charset=utf-8") -> list[bytes]:
        data = body.encode("utf-8")
        start_response(status, [("Content-Type", content_type),
                                ("Content-Length", str(len(data)))])
        return [data]

    def _serve_design(self, did: str, start_response: Callable) -> list[bytes]:
        with self._lock:
            window = self._designs.get(did)
            expired = window is None or float(window["expires_at"]) < time.time()
        if window is None or expired:
            return self._respond(start_response, "404 Not Found",
                                 "<h1>窗口已过期</h1><p>设计渲染窗口最多存在 1 分钟，"
                                 "让 bot 重新 design_render 一次即可。</p>")
        return self._respond(start_response, "200 OK", window["content"], window["type"])

    def _serve_program(self, pid: str, sub_path: str,
                       environ: dict, start_response: Callable) -> list[bytes]:
        with self._lock:
            proc = self._apps.get(pid)
        if proc is None:
            running = sorted(self._apps)
            return self._respond(start_response, "404 Not Found",
                                 f"<h1>程序 {pid} 未在运行</h1>"
                                 f"<p>可能已过期或未启动。当前运行中：{running or '无'}。"
                                 "让 bot 用 program_write 重新启动。</p>")
        environ["SCRIPT_NAME"] = f"/p/{pid}"
        environ["PATH_INFO"] = sub_path or "/"
        try:
            return proc["app"](environ, start_response)
        except Exception as error:
            return self._respond(start_response, "500 Internal Server Error",
                                 f"<h1>程序内部错误</h1><pre>{type(error).__name__}: "
                                 f"{str(error)[:600]}</pre>")

    def _serve_index(self, start_response: Callable) -> list[bytes]:
        with self._lock:
            running = [(pid, proc["title"]) for pid, proc in sorted(self._apps.items())]
            live = sorted(self._designs)
        rows = "".join(
            f'<li><a href="/p/{pid}/">{pid}</a> — {title or ""}</li>' for pid, title in running
        ) or "<li>（没有运行中的程序）</li>"
        designs = "".join(f'<li><a href="/d/{d}/">{d}</a></li>' for d in live) \
            or "<li>（没有存活的渲染窗口）</li>"
        return self._respond(start_response, "200 OK",
                             f"<h1>studio</h1><h2>运行中的程序</h2><ul>{rows}</ul>"
                             f"<h2>存活的设计窗口（60 秒内）</h2><ul>{designs}</ul>")

    # ------------------------------------------------------------ designer
    def put_design(self, content: str, content_type: str = "text/html") -> dict[str, Any]:
        did = uuid.uuid4().hex[:10]
        with self._lock:
            self._sweep_designs_locked()
            self._designs[did] = {
                "content": str(content), "type": content_type,
                "expires_at": time.time() + DESIGN_TTL_SECONDS,
            }
        return {
            "design_id": did,
            "url": f"http://127.0.0.1:{self.port}/d/{did}",
            "expires_in_seconds": int(DESIGN_TTL_SECONDS),
        }

    def _sweep_designs_locked(self) -> int:
        now = time.time()
        expired = [key for key, win in self._designs.items()
                   if float(win["expires_at"]) < now]
        for key in expired:
            self._designs.pop(key, None)
        return len(expired)

    # ------------------------------------------------------------ programmer
    def data_dir(self, program_id: str) -> Path:
        path = self.root / "programs" / program_id / "data"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def build_app(self, program_id: str, code: str) -> tuple[Any, str]:
        """Exec bot code in a fresh namespace and extract the WSGI ``app``.

        Injected names: PROGRAM_ID, DATA_DIR. The bot's code defines
        ``app = Flask(__name__)`` with its own routes (multi-page by itself).
        """
        ok, detail = ensure_flask()
        if not ok:
            return None, detail
        data_dir = self.data_dir(program_id)
        namespace: dict[str, Any] = {
            "PROGRAM_ID": program_id,
            "DATA_DIR": str(data_dir),
            "__name__": f"program_{program_id}",
        }
        try:
            exec(compile(str(code), f"<program:{program_id}>", "exec"), namespace)
        except Exception as error:
            return None, f"代码执行失败：{type(error).__name__}: {error}"
        app = namespace.get("app")
        if app is None or not callable(app):
            return None, ("代码里没有定义可调用的 app（需要 `app = Flask(__name__)` "
                          "并挂上 @app.route 路由）")
        return app, ""

    def run_program(self, program_id: str, code: str, title: str = "") -> dict[str, Any]:
        app, error = self.build_app(program_id, code)
        if app is None:
            return {"ok": False, "error": error}
        with self._lock:
            self._apps[program_id] = {
                "app": app, "started_at": time.time(),
                "data_dir": str(self.data_dir(program_id)), "title": title,
            }
        return {
            "ok": True, "program_id": program_id,
            "url": f"http://127.0.0.1:{self.port}/p/{program_id}/",
            "data_dir": str(self.data_dir(program_id)),
            "ttl_minutes": round(self.program_ttl / 60, 1),
        }

    def stop_program(self, program_id: str) -> bool:
        with self._lock:
            return self._apps.pop(program_id, None) is not None

    def running(self) -> list[dict[str, Any]]:
        with self._lock:
            now = time.time()
            return [
                {
                    "program_id": pid,
                    "title": str(proc.get("title") or ""),
                    "url": f"http://127.0.0.1:{self.port}/p/{pid}/",
                    "expires_in_minutes": round(max(0.0, self.program_ttl - (now - float(proc["started_at"]))) / 60, 1),
                    "data_dir": str(proc.get("data_dir") or ""),
                }
                for pid, proc in sorted(self._apps.items())
            ]

    def sweep(self) -> list[str]:
        """Unload expired programs + expired design windows; returns expired ids."""
        with self._lock:
            now = time.time()
            expired = [pid for pid, proc in self._apps.items()
                       if now - float(proc["started_at"]) > self.program_ttl]
            for pid in expired:
                self._apps.pop(pid, None)
                self.log(f"程序 {pid} 已超过 {self.program_ttl / 60:.0f} 分钟，已停止（代码与数据保留，可 program_write 重启）")
            self._sweep_designs_locked()
            return expired

    # ------------------------------------------------------------ registry
    def _load_registry(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.registry_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def _save_registry(self) -> None:
        try:
            self.registry_path.write_text(
                json.dumps(self.registry, ensure_ascii=False, indent=1),
                encoding="utf-8")
        except OSError as error:
            self.log(f"registry 落盘失败：{error}")

    def _find(self, kind: str, item_id: str) -> dict[str, Any] | None:
        return next((row for row in self.registry
                     if row.get("kind") == kind and row.get("id") == item_id), None)

    def upsert_program(self, program_id: str, code: str,
                       title: str = "", description: str = "") -> dict[str, Any]:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._lock:
            row = self._find("program", program_id)
            created = row is None
            if row is None:
                row = {"kind": "program", "id": program_id, "created_at": now,
                       "title": "", "description": "", "archived": False}
                self.registry.append(row)
            row["code"] = str(code)
            row["updated_at"] = now
            row["archived"] = False
            if title.strip():
                row["title"] = title.strip()[:120]
            if description.strip():
                row["description"] = description.strip()[:600]
            self._save_registry()
            return {"row": dict(row), "created": created}

    def get_program(self, program_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._find("program", program_id)
            return dict(row) if row else None

    def list_programs(self, archived: bool | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = [dict(row) for row in self.registry if row.get("kind") == "program"]
        if archived is not None:
            rows = [row for row in rows if bool(row.get("archived")) is archived]
        for row in rows:
            row.pop("code", None)
        return rows

    def archive_program(self, program_id: str, title: str = "",
                        description: str = "") -> dict[str, Any]:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._lock:
            row = self._find("program", program_id)
            if row is None:
                return {}
            row["archived"] = True
            row["updated_at"] = now
            if title.strip():
                row["title"] = title.strip()[:120]
            if description.strip():
                row["description"] = description.strip()[:600]
            self._apps.pop(program_id, None)
            self._save_registry()
            return dict(row)

    # designer 项目
    def save_design_project(self, title: str, description: str,
                            content: str, content_type: str = "text/html") -> dict[str, Any]:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        pid = slugify(title, fallback=f"design-{uuid.uuid4().hex[:6]}")
        with self._lock:
            row = self._find("design", pid)
            if row is None:
                row = {"kind": "design", "id": pid, "created_at": now}
                self.registry.append(row)
            row.update({
                "title": title.strip()[:120], "description": description.strip()[:600],
                "content": str(content), "content_type": content_type,
                "updated_at": now,
            })
            self._save_registry()
            return dict(row)

    def list_design_projects(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = [dict(row) for row in self.registry if row.get("kind") == "design"]
        for row in rows:
            row.pop("content", None)
        return rows

    def get_design_project(self, project_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._find("design", project_id)
            return dict(row) if row else None

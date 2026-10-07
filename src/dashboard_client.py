from __future__ import annotations

import ipaddress
import json
import socket
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit


class DashboardError(RuntimeError):
    """A safe Dashboard failure that never includes credentials."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class DashboardClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_seconds: float = 8.0,
        allow_remote_dashboard: bool = False,
        allow_arbitrary_plugin_urls: bool = False,
        plugin_url_host_allowlist: Any = (),
        session: Any | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.timeout_seconds = max(0.1, timeout_seconds)
        self.allow_remote_dashboard = allow_remote_dashboard
        self.allow_arbitrary_plugin_urls = allow_arbitrary_plugin_urls
        hosts = (
            (plugin_url_host_allowlist,)
            if isinstance(plugin_url_host_allowlist, str)
            else plugin_url_host_allowlist
        )
        self.plugin_url_host_allowlist = frozenset(
            str(host).strip().rstrip(".").casefold()
            for host in hosts
            if str(host).strip()
        )
        self._session = session
        self._validate_base_url()

    def _validate_base_url(self) -> None:
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Dashboard base URL must be HTTP(S)")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Dashboard base URL must not contain credentials, query, or fragment")
        if not self.allow_remote_dashboard and not self._is_loopback_host(parsed.hostname):
            raise ValueError("Remote Dashboard requires allow_remote_dashboard=True")

    @staticmethod
    def _is_loopback_host(host: str) -> bool:
        normalized = host.rstrip(".").casefold()
        if normalized == "localhost":
            return True
        try:
            return ipaddress.ip_address(normalized).is_loopback
        except ValueError:
            return False

    @staticmethod
    def _blocked_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        return not address.is_global

    def _validate_plugin_url(self, url: str) -> None:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").rstrip(".").casefold()
        if parsed.scheme != "https" or not host or parsed.username or parsed.password:
            raise ValueError("Plugin URL must be credential-free HTTPS")
        if host not in self.plugin_url_host_allowlist:
            raise PermissionError("Plugin URL host is not allowlisted")
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            if self._blocked_ip(literal):
                raise PermissionError("Plugin URL resolves to a non-public address")
            return
        try:
            addresses = {
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
            }
        except (OSError, ValueError) as exc:
            raise PermissionError("Plugin URL host could not be safely resolved") from exc
        if not addresses or any(self._blocked_ip(address) for address in addresses):
            raise PermissionError("Plugin URL resolves to a non-public address")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        data: Any = None,
    ) -> Any:
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        kwargs: dict[str, Any] = {
            "headers": headers,
            "params": params,
            "allow_redirects": False,
        }
        if json_body is not None:
            kwargs["json"] = json_body
        if data is not None:
            kwargs["data"] = data
        try:
            import aiohttp
        except ImportError as exc:
            raise DashboardError("aiohttp is required for Dashboard requests") from exc
        kwargs["timeout"] = aiohttp.ClientTimeout(total=self.timeout_seconds)
        owns_session = self._session is None
        session = self._session
        try:
            if session is None:
                session = aiohttp.ClientSession()
            async with session.request(method, self.base_url + path, **kwargs) as response:
                text = await response.text()
                try:
                    payload = json.loads(text) if text else {}
                except json.JSONDecodeError as exc:
                    raise DashboardError(
                        f"Dashboard returned non-JSON response (HTTP {response.status})",
                        status=response.status,
                    ) from exc
                if response.status < 200 or response.status >= 300:
                    raise DashboardError(
                        self._error_message(payload, f"Dashboard HTTP {response.status}"),
                        status=response.status,
                    )
                if isinstance(payload, Mapping) and payload.get("status") == "error":
                    raise DashboardError(
                        self._error_message(payload, "Dashboard operation failed"),
                        status=response.status,
                    )
                return payload
        except DashboardError:
            raise
        except Exception as exc:
            client_error = getattr(aiohttp, "ClientError", ())
            if isinstance(exc, (client_error, TimeoutError)):
                raise DashboardError(
                    f"Dashboard request failed: {type(exc).__name__}"
                ) from exc
            raise
        finally:
            if owns_session and session is not None:
                await session.close()

    @staticmethod
    def _error_message(payload: Any, fallback: str) -> str:
        if isinstance(payload, Mapping):
            value = payload.get("message") or payload.get("detail")
            if value:
                return str(value)[:1000]
        return fallback

    @staticmethod
    def unwrap(payload: Any) -> Any:
        if isinstance(payload, Mapping) and payload.get("status") == "ok" and "data" in payload:
            return payload["data"]
        return payload

    async def list_plugins(self) -> Any:
        return self.unwrap(await self._request("GET", "/api/v1/plugins"))

    async def plugin_detail(self, plugin_id: str) -> Any:
        return self.unwrap(
            await self._request(
                "GET", "/api/v1/plugins/by-id", params={"plugin_id": plugin_id}
            )
        )

    async def failed_plugins(self) -> Any:
        return self.unwrap(await self._request("GET", "/api/v1/plugins/failed"))

    async def list_market(self, *, force_refresh: bool = False) -> Any:
        payload = await self._request(
            "GET",
            "/api/v1/plugins/market",
            params={"force_refresh": str(force_refresh).lower()},
        )
        return self.unwrap(payload)

    async def search_market(self, query: str) -> list[dict[str, Any]]:
        """Fetch the complete market once and filter it locally."""
        market = await self.list_market()
        entries = self._market_entries(market)
        words = [word.casefold() for word in query.split() if word]
        if not words:
            return entries
        return [
            entry
            for entry in entries
            if all(word in self._searchable_text(entry) for word in words)
        ]

    @classmethod
    def _market_entries(cls, market: Any) -> list[dict[str, Any]]:
        if isinstance(market, list):
            return [dict(item) for item in market if isinstance(item, Mapping)]
        if not isinstance(market, Mapping):
            return []
        for key in ("plugins", "items", "results", "data"):
            nested = market.get(key)
            if nested is market:
                continue
            found = cls._market_entries(nested)
            if found:
                return found
        result = []
        for key, item in market.items():
            if isinstance(item, Mapping):
                entry = dict(item)
                entry.setdefault("market_plugin_id", str(key))
                result.append(entry)
        return result

    @staticmethod
    def _searchable_text(entry: Mapping[str, Any]) -> str:
        fields = (
            "market_plugin_id", "id", "name", "display_name", "author", "desc",
            "description", "short_desc", "repo", "tags", "categories",
        )
        return " ".join(str(entry.get(field, "")) for field in fields).casefold()

    async def install_market_plugin(
        self, market_plugin_id: str, *, ignore_version_check: bool = False
    ) -> Any:
        entry = await self._find_market_plugin(market_plugin_id)
        repository = str(entry.get("repo") or "").strip()
        if not repository:
            raise DashboardError("Marketplace plugin has no repository URL")
        body: dict[str, Any] = {
            "repository": repository,
            "market_plugin_id": market_plugin_id,
            "ignore_version_check": ignore_version_check,
        }
        if entry.get("download_url"):
            body["download_url"] = entry["download_url"]
        return self.unwrap(
            await self._request("POST", "/api/v1/plugins/install/github", json_body=body)
        )

    async def install_plugin_url(self, url: str) -> Any:
        if not self.allow_arbitrary_plugin_urls:
            raise PermissionError("Arbitrary plugin URL installation is disabled")
        self._validate_plugin_url(url)
        return self.unwrap(
            await self._request(
                "POST", "/api/v1/plugins/install/url", json_body={"url": url}
            )
        )

    async def _find_market_plugin(self, market_plugin_id: str) -> dict[str, Any]:
        for entry in self._market_entries(await self.list_market()):
            if str(entry.get("market_plugin_id") or "") == market_plugin_id:
                return entry
        raise DashboardError("Marketplace plugin not found")

    async def update_plugin(self, plugin_id: str) -> Any:
        return self.unwrap(
            await self._request(
                "POST", "/api/v1/plugins/update", json_body={"plugin_id": plugin_id}
            )
        )

    async def set_plugin_enabled(self, plugin_id: str, enabled: bool) -> Any:
        return self.unwrap(
            await self._request(
                "PATCH",
                "/api/v1/plugins/enabled",
                json_body={"plugin_id": plugin_id, "enabled": enabled},
            )
        )

    async def enable_plugin(self, plugin_id: str) -> Any:
        return await self.set_plugin_enabled(plugin_id, True)

    async def disable_plugin(self, plugin_id: str) -> Any:
        return await self.set_plugin_enabled(plugin_id, False)

    async def reload_plugin(self, plugin_id: str) -> Any:
        return self.unwrap(
            await self._request(
                "POST", "/api/v1/plugins/reload", json_body={"plugin_id": plugin_id}
            )
        )

    async def list_skills(self) -> Any:
        return self.unwrap(await self._request("GET", "/api/v1/skills"))

    async def upload_skill(self, archive: bytes, filename: str) -> Any:
        try:
            import aiohttp
        except ImportError as exc:
            raise DashboardError("aiohttp is required for Dashboard requests") from exc
        form = aiohttp.FormData()
        form.add_field("file", archive, filename=filename, content_type="application/zip")
        return self.unwrap(await self._request("POST", "/api/v1/skills", data=form))

    async def read_skill_file(self, skill_name: str, path: str = "SKILL.md") -> Any:
        return self.unwrap(
            await self._request(
                "GET",
                "/api/v1/skills/file",
                params={"skill_name": skill_name, "path": path},
            )
        )

    async def update_skill_file(
        self, skill_name: str, content: str, path: str = "SKILL.md"
    ) -> Any:
        return self.unwrap(
            await self._request(
                "PUT",
                "/api/v1/skills/file",
                json_body={"skill_name": skill_name, "path": path, "content": content},
            )
        )

    async def set_skill_enabled(self, skill_name: str, enabled: bool) -> Any:
        return self.unwrap(
            await self._request(
                "PATCH",
                "/api/v1/skills/by-name",
                json_body={"skill_name": skill_name, "active": enabled},
            )
        )

    async def enable_skill(self, skill_name: str) -> Any:
        return await self.set_skill_enabled(skill_name, True)

    async def disable_skill(self, skill_name: str) -> Any:
        return await self.set_skill_enabled(skill_name, False)

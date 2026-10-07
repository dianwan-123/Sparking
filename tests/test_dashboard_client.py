from __future__ import annotations

import asyncio
import json
import unittest
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.modules.setdefault("aiohttp", SimpleNamespace(ClientTimeout=lambda **kwargs: kwargs, ClientError=OSError, ClientSession=None, FormData=None))

from src.dashboard_client import DashboardClient, DashboardError


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    async def text(self):
        return self.payload if isinstance(self.payload, str) else json.dumps(self.payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class DashboardClientTests(unittest.TestCase):
    def test_bearer_and_unwrap(self):
        session = FakeSession([FakeResponse({"status": "ok", "data": [{"id": "p"}]})])
        client = DashboardClient("http://localhost:6185", "top-secret", session=session)
        result = asyncio.run(client.list_plugins())
        self.assertEqual(result, [{"id": "p"}])
        self.assertEqual(session.calls[0][2]["headers"]["Authorization"], "Bearer top-secret")
        self.assertFalse(session.calls[0][2]["allow_redirects"])
        self.assertNotIn("top-secret", repr(client.__dict__.keys()))

    def test_non_json_and_error_envelope(self):
        client = DashboardClient(
            "http://localhost", "key", session=FakeSession([FakeResponse("html", 502)])
        )
        with self.assertRaises(DashboardError) as raised:
            asyncio.run(client.list_plugins())
        self.assertEqual(raised.exception.status, 502)
        self.assertNotIn("key", str(raised.exception))

    def test_market_search_fetches_all_then_filters_locally(self):
        session = FakeSession([
            FakeResponse({"status": "ok", "data": {"a/x": {"name": "weather", "desc": "Rain"}, "b/y": {"name": "chat"}}})
        ])
        client = DashboardClient("http://localhost", "", session=session)
        self.assertEqual(asyncio.run(client.search_market("WEATHER rain"))[0]["name"], "weather")
        self.assertEqual(len(session.calls), 1)
        self.assertNotIn("query", session.calls[0][2]["params"])

    def test_install_market_id_uses_market_record(self):
        session = FakeSession([
            FakeResponse({"status": "ok", "data": [{"market_plugin_id": "a/x", "name": "x", "author": "a", "repo": "https://github.com/a/x"}]}),
            FakeResponse({"status": "ok", "data": {"installed": True}}),
        ])
        client = DashboardClient("http://localhost", "", session=session)
        result = asyncio.run(client.install_market_plugin("a/x"))
        self.assertTrue(result["installed"])
        body = session.calls[1][2]["json"]
        self.assertEqual(body["market_plugin_id"], "a/x")
        self.assertEqual(session.calls[1][1], "http://localhost/api/v1/plugins/install/github")

    def test_arbitrary_url_is_default_denied_without_request(self):
        session = FakeSession([])
        client = DashboardClient("http://localhost", "", session=session)
        with self.assertRaises(PermissionError):
            asyncio.run(client.install_plugin_url("https://example.test/plugin.zip"))
        self.assertEqual(session.calls, [])

    def test_remote_dashboard_requires_explicit_opt_in(self):
        with self.assertRaises(ValueError):
            DashboardClient("https://dashboard.example", "key")
        client = DashboardClient(
            "https://dashboard.example", "key", allow_remote_dashboard=True,
            session=FakeSession([]),
        )
        self.assertEqual(client.base_url, "https://dashboard.example")

    def test_market_install_requires_exact_market_id(self):
        session = FakeSession([
            FakeResponse({"status": "ok", "data": [{"name": "x", "author": "a", "repo": "https://github.com/a/x"}]})
        ])
        client = DashboardClient("http://localhost", "", session=session)
        with self.assertRaises(DashboardError):
            asyncio.run(client.install_market_plugin("a/x"))
        self.assertEqual(len(session.calls), 1)

    @patch("src.dashboard_client.socket.getaddrinfo")
    def test_arbitrary_url_requires_allowlisted_public_host(self, resolve):
        resolve.return_value = [(2, 1, 6, "", ("93.184.216.34", 443))]
        session = FakeSession([FakeResponse({"status": "ok", "data": {}})])
        client = DashboardClient(
            "http://127.0.0.1", "", allow_arbitrary_plugin_urls=True,
            plugin_url_host_allowlist={"plugins.example"}, session=session,
        )
        asyncio.run(client.install_plugin_url("https://plugins.example/p.zip"))
        with self.assertRaises(PermissionError):
            asyncio.run(client.install_plugin_url("https://evil.example/p.zip"))

    @patch("src.dashboard_client.socket.getaddrinfo")
    def test_arbitrary_url_rejects_private_and_metadata_addresses(self, resolve):
        client = DashboardClient(
            "http://127.0.0.1", "", allow_arbitrary_plugin_urls=True,
            plugin_url_host_allowlist={"plugins.example", "169.254.169.254"},
            session=FakeSession([]),
        )
        resolve.return_value = [(2, 1, 6, "", ("10.0.0.5", 443))]
        with self.assertRaises(PermissionError):
            asyncio.run(client.install_plugin_url("https://plugins.example/p.zip"))
        with self.assertRaises(PermissionError):
            asyncio.run(client.install_plugin_url("https://169.254.169.254/latest"))

    def test_plugin_and_skill_operation_shapes(self):
        session = FakeSession([FakeResponse({"status": "ok", "data": {}}) for _ in range(4)])
        client = DashboardClient("http://localhost", "", session=session)
        asyncio.run(client.update_plugin("plugin-x"))
        asyncio.run(client.disable_plugin("plugin-x"))
        asyncio.run(client.reload_plugin("plugin-x"))
        asyncio.run(client.set_skill_enabled("my-skill", True))
        self.assertEqual(session.calls[0][2]["json"], {"plugin_id": "plugin-x"})
        self.assertEqual(session.calls[1][0:2], ("PATCH", "http://localhost/api/v1/plugins/enabled"))
        self.assertEqual(session.calls[2][1], "http://localhost/api/v1/plugins/reload")
        self.assertEqual(session.calls[3][2]["json"], {"skill_name": "my-skill", "active": True})


if __name__ == "__main__":
    unittest.main()

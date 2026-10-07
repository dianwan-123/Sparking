from __future__ import annotations

import asyncio
import hashlib
import json
import time
import urllib.parse
from typing import Any, Awaitable, Callable

from .qq_gateway import QQGateway, QQGatewayError, _json_safe

_QZONE_DOMAIN = "user.qzone.qq.com"


def _parse_cookie_pairs(cookie_text: str) -> dict[str, str]:
    """Parse 'k=v; k2=v2' tolerating stray whitespace around names and values."""
    result: dict[str, str] = {}
    for part in (cookie_text or "").replace("\n", ";").split(";"):
        if "=" not in part:
            continue
        name, _, value = part.strip().partition("=")
        name = name.strip()
        if name:
            result[name] = value.strip()
    return result


def _g_tk(skey: str) -> int:
    seed = 5381
    for char in skey or "":
        seed += (seed << 5) + ord(char)
    return seed & 0x7FFFFFFF


class QQService:
    """LLM-managed social operations; every call funnels through the gateway."""

    def __init__(self, gateway: QQGateway) -> None:
        self.gateway = gateway

    async def friend_list(self) -> Any:
        return _json_safe(await self.gateway.execute("get_friend_list"))

    async def group_list(self) -> Any:
        return _json_safe(await self.gateway.execute("get_group_list"))

    async def group_members(self, group_id: str | int) -> Any:
        return _json_safe(await self.gateway.execute("get_group_member_list", group_id=int(group_id)))

    async def group_member_info(self, group_id: str | int, user_id: str | int) -> Any:
        return _json_safe(await self.gateway.execute(
            "get_group_member_info", group_id=int(group_id), user_id=int(user_id)))

    async def stranger_info(self, user_id: str | int) -> Any:
        return _json_safe(await self.gateway.execute("get_stranger_info", user_id=int(user_id)))

    async def login_info(self) -> Any:
        return _json_safe(await self.gateway.execute("get_login_info"))

    async def group_history(self, group_id: str | int, count: int = 20, message_seq: int | None = None) -> Any:
        params: dict[str, Any] = {"group_id": int(group_id), "count": min(max(count, 1), 60)}
        if message_seq:
            params["message_seq"] = int(message_seq)
        return _json_safe(await self.gateway.execute("get_group_msg_history", **params))

    async def friend_history(self, user_id: str | int, count: int = 20) -> Any:
        return _json_safe(await self.gateway.execute(
            "get_friend_msg_history", user_id=int(user_id), count=min(max(count, 1), 60)))

    async def handle_friend_request(self, flag: str, approve: bool = True, remark: str = "") -> Any:
        return await self.gateway.execute(
            "set_friend_add_request", flag=str(flag)[:128], approve=bool(approve), remark=str(remark)[:64])

    async def handle_group_invite(self, flag: str, approve: bool = True, reason: str = "") -> Any:
        return await self.gateway.execute(
            "set_group_add_request", flag=str(flag)[:128], sub_type="invite",
            approve=bool(approve), reason=str(reason)[:64])

    async def delete_friend(self, user_id: str | int) -> Any:
        return await self.gateway.execute("delete_friend", user_id=int(user_id))

    async def leave_group(self, group_id: str | int) -> Any:
        return await self.gateway.execute("set_group_leave", group_id=int(group_id))

    async def ban_member(self, group_id: str | int, user_id: str | int, duration: int = 60) -> Any:
        return await self.gateway.execute(
            "set_group_ban", group_id=int(group_id), user_id=int(user_id),
            duration=min(max(int(duration), 1), 2592000))

    async def set_group_card(self, group_id: str | int, user_id: str | int, card: str) -> Any:
        return await self.gateway.execute(
            "set_group_card", group_id=int(group_id), user_id=int(user_id), card=str(card)[:60])

    async def set_group_name(self, group_id: str | int, group_name: str) -> Any:
        return await self.gateway.execute(
            "set_group_name", group_id=int(group_id), group_name=str(group_name)[:30])

    async def set_special_title(self, group_id: str | int, user_id: str | int, title: str) -> Any:
        return await self.gateway.execute(
            "set_group_special_title", group_id=int(group_id), user_id=int(user_id),
            special_title=str(title)[:30])

    async def set_profile(self, *, nickname: str | None = None, longnick: str | None = None) -> Any:
        results = []
        if nickname:
            results.append(await self.gateway.execute("set_qq_profile", nickname=str(nickname)[:24]))
        if longnick is not None:
            results.append(await self.gateway.execute("set_self_longnick", longNick=str(longnick)[:120]))
        return results

    async def set_online_status(self, status: int = 0, ext_status: int = 0) -> Any:
        return await self.gateway.execute(
            "set_online_status", status=int(status), ext_status=int(ext_status), battery_status=0)

    async def set_avatar(self, file: str) -> Any:
        return await self.gateway.execute("set_qq_avatar", file=str(file))

    async def recall(self, message_id: str | int) -> Any:
        return await self.gateway.execute("delete_msg", message_id=int(message_id))

    async def poke(self, user_id: str | int, group_id: str | int | None = None) -> Any:
        if group_id:
            return await self.gateway.execute(
                "group_poke", group_id=int(group_id), user_id=int(user_id))
        return await self.gateway.execute("friend_poke", user_id=int(user_id))

    async def send_group(self, group_id: str | int, message: list[dict[str, Any]]) -> Any:
        return await self.gateway.execute(
            "send_group_msg", group_id=int(group_id), message=message)

    async def send_private(self, user_id: str | int, message: list[dict[str, Any]]) -> Any:
        return await self.gateway.execute(
            "send_private_msg", user_id=int(user_id), message=message)

    async def send_group_forward(self, group_id: str | int, nodes: list[dict[str, Any]]) -> Any:
        return await self.gateway.execute(
            "send_group_forward_msg", group_id=int(group_id), messages=nodes)

    async def recommend(self, *, user_id: str | int | None = None, group_id: str | int | None = None) -> dict[str, Any]:
        if user_id:
            segment = {"type": "contact", "data": {"type": "qq", "id": str(user_id)}}
        elif group_id:
            segment = {"type": "contact", "data": {"type": "group", "id": str(group_id)}}
        else:
            raise ValueError("user_id or group_id required")
        return {"segment": segment}


class QzoneError(RuntimeError):
    pass


class QzoneService:
    """Qzone feed operations over a gateway-backed cookie session (LLM-managed)."""

    def __init__(
        self,
        gateway: QQGateway,
        poster: Callable[[str, str, dict[str, Any]], Awaitable[Any]] | None = None,
        getter: Callable[[str, str, dict[str, Any]], Awaitable[Any]] | None = None,
        ttl_seconds: int = 1800,
    ) -> None:
        self.gateway = gateway
        self._poster = poster or _default_poster
        self._getter = getter or _default_getter
        self.ttl_seconds = max(60, int(ttl_seconds))
        self._session: dict[str, Any] = {}
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()

    async def _refresh(self) -> None:
        cookie_text = ""
        bkn = ""
        for domain in ("user.qzone.qq.com", "qzone.qq.com"):
            try:
                cookies_payload = await self.gateway.execute("get_cookies", domain=domain)
            except QQGatewayError:
                continue
            payload = cookies_payload.get("data", cookies_payload) if isinstance(cookies_payload, dict) else {}
            if isinstance(payload, dict):
                cookie_text = str(payload.get("cookies") or "")
                bkn = str(payload.get("bkn") or "")
                if cookie_text:
                    break
        if not cookie_text:
            try:
                credentials = await self.gateway.execute("get_credentials")
            except QQGatewayError:
                credentials = None
            payload = credentials.get("data", credentials) if isinstance(credentials, dict) else {}
            if isinstance(payload, dict):
                cookie_text = str(payload.get("cookies") or "")
                bkn = str(payload.get("token") or "")
        parsed = _parse_cookie_pairs(cookie_text)
        login = await self.gateway.execute("get_login_info")
        uin = ""
        if isinstance(login, dict):
            uin = str(login.get("user_id") or login.get("uin") or "")
        skey = parsed.get("p_skey") or parsed.get("skey") or ""
        if not uin or not skey:
            raise QzoneError(
                "Qzone cookies unavailable: got cookie keys "
                f"{sorted(parsed) or 'none'} and uin {'ok' if uin else 'missing'}; "
                "check that the bot account is logged in and can access Qzone"
            )
        g_tk = int(bkn) if bkn.isdigit() else _g_tk(skey)
        self._session = {"uin": uin, "cookie": cookie_text, "g_tk": g_tk}
        self._fetched_at = time.monotonic()

    async def _ensure_session(self) -> dict[str, Any]:
        async with self._lock:
            if not self._session or time.monotonic() - self._fetched_at > self.ttl_seconds:
                await self._refresh()
            return self._session

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        session = await self._ensure_session()
        payload = {"hostUin": session["uin"], "uin": session["uin"], "g_tk": session["g_tk"], **params}
        try:
            return await self._getter(path, session["cookie"], payload)
        except QzoneError as error:
            if "auth" not in str(error).lower() and "401" not in str(error):
                raise
            await self._refresh()
            session = self._session
            payload = {"hostUin": session["uin"], "uin": session["uin"], "g_tk": session["g_tk"], **params}
            return await self._getter(path, session["cookie"], payload)

    async def _post(self, path: str, data: dict[str, Any]) -> Any:
        session = await self._ensure_session()
        payload = {"hostUin": session["uin"], "uin": session["uin"], "g_tk": session["g_tk"], **data}
        try:
            return await self._poster(path, session["cookie"], payload)
        except QzoneError as error:
            if "auth" not in str(error).lower() and "401" not in str(error):
                raise
            await self._refresh()
            session = self._session
            payload = {"hostUin": session["uin"], "uin": session["uin"], "g_tk": session["g_tk"], **data}
            return await self._poster(path, session["cookie"], payload)

    async def list_feeds(self, count: int = 10, uin: str | None = None) -> Any:
        try:
            native = await self.gateway.execute(
                "get_qzone_msg_list", count=min(max(count, 1), 20)
            )
            data = native.get("data", native) if isinstance(native, dict) else native
            if data:
                return _json_safe(data)
        except QQGatewayError:
            pass
        session = await self._ensure_session()
        target_uin = str(uin or session["uin"])
        result = await self._get(
            "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6",
            {"uin": target_uin, "pos": 0, "num": min(max(count, 1), 20),
             "replynum": 10, "need_comment": 1, "g_tk": session["g_tk"]},
        )
        return _json_safe(result)

    async def publish(self, text: str, images: list[str] | None = None) -> dict[str, Any]:
        text = str(text).strip()
        if not text or len(text) > 1000:
            raise QzoneError("publish text must be 1-1000 characters")
        try:
            params: dict[str, Any] = {"content": text}
            if images:
                params["images"] = [str(x)[:500] for x in images[:9]]
            result = await self.gateway.execute("send_qzone_msg", **params)
            tid = ""
            if isinstance(result, dict):
                tid = str(result.get("tid") or (result.get("data") or {}).get("tid", ""))
            return {"tid": tid, "text": text[:200], "via": "napcat"}
        except QQGatewayError:
            pass
        pic_bo = ""
        richval = ""
        con = text
        for index, image in enumerate((images or [])[:9]):
            upload = await self._upload_image(image)
            pic_bo += "USDhUsl5bZVUVuVtWbVVVrVVdRUVpRVtRUVpRbTVWVtUbTVrrVdtUbTVo1VVpRVvTVs1VVrR1CrbzkAAA..,"
            richval += f",{upload.get('pic_id', '')},{upload.get('width', 0)},{upload.get('height', 0)},{upload.get('album_id', '')}"
        result = await self._post(
            "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_publish_v6",
            {
                "con": con + (f" {richval}" if richval else ""),
                "richtype": "1" if richval else "0",
                "richval": richval.lstrip(","),
                "pic_bo": pic_bo.rstrip(","),
                "ugc_right": "1",
                "to_sign": "0",
                "anonymousType": "0",
                "qzreferrer": "https://user.qzone.qq.com/",
            },
        )
        tid = ""
        if isinstance(result, dict):
            tid = str(result.get("tid") or result.get("data", {}).get("tid", ""))
        return {"tid": tid, "text": text[:200]}

    async def _upload_image(self, source: str) -> dict[str, Any]:
        session = await self._ensure_session()
        try:
            encoded = await asyncio.to_thread(_read_image_base64, source)
        except OSError as error:
            raise QzoneError(f"cannot read image: {error}") from error
        result = await self._post(
            "https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image",
            {
                "filename": "sticker.png",
                "uploadtype": "1",
                "albumtype": "7",
                "base64": "1",
                "picfile": encoded,
                "output_type": "json",
            },
        )
        return result if isinstance(result, dict) else {}

    async def like(self, unikey: str, curkey: str, owner_uin: str | int) -> Any:
        return await self._post(
            "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app",
            {
                "opuin": str(owner_uin), "unikey": str(unikey), "curkey": str(curkey),
                "appid": "311", "abstime": str(int(time.time())), "fid": str(unikey),
                "opuin_uin": "1", "from": "1",
            },
        )

    async def comment(self, topic_id: str, content: str, feeds_type: str = "0") -> Any:
        content = str(content).strip()
        if not content or len(content) > 500:
            raise QzoneError("comment must be 1-500 characters")
        session = await self._ensure_session()
        return await self._post(
            "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_re_feeds",
            {
                "topicId": str(topic_id), "feedsType": str(feeds_type),
                "content": content, "hostUin": session["uin"], "uin": session["uin"],
            },
        )

    async def reply_comment(self, topic_id: str, content: str, comment_id: str, comment_uin: str) -> Any:
        session = await self._ensure_session()
        return await self._post(
            "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_re_feeds",
            {
                "topicId": str(topic_id), "content": str(content)[:500],
                "commentId": str(comment_id), "commentUin": str(comment_uin),
                "feedsType": "0", "hostUin": session["uin"], "uin": session["uin"],
                "private": "0",
            },
        )

    async def delete(self, topic_id: str) -> Any:
        try:
            return await self.gateway.execute("delete_qzone_msg", tid=str(topic_id))
        except QQGatewayError:
            pass
        session = await self._ensure_session()
        return await self._post(
            "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_delete_v6",
            {"topicId": str(topic_id), "feedsAppid": "311", "uin": session["uin"]},
        )


def _read_image_base64(source: str) -> str:
    from pathlib import Path

    path = Path(source).resolve()
    if path.stat().st_size > 8 * 1024 * 1024:
        raise QzoneError("image exceeds 8MiB upload limit")
    return base64.b64encode(path.read_bytes()).decode("ascii")


async def _default_getter(path: str, cookie: str, params: dict[str, Any]) -> Any:
    import aiohttp

    headers = {
        "Cookie": cookie,
        "Referer": "https://user.qzone.qq.com/",
    }
    try:
        async with asyncio.timeout(20):
            async with aiohttp.ClientSession() as session:
                async with session.get(path, params=params, headers=headers) as response:
                    text = await response.text()
                    if response.status in (301, 302, 401, 403):
                        raise QzoneError(f"auth challenge: HTTP {response.status}")
                    if response.status != 200:
                        raise QzoneError(f"HTTP {response.status}")
    except TimeoutError as error:
        raise QzoneError("qzone request timed out") from error
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start >= 0 and "_Callback(" in text[:start]:
            inner = text[text.index("_Callback(") + 10:].rstrip(");")
            try:
                return json.loads(inner)
            except json.JSONDecodeError:
                pass
        return {"raw": text[:2000]}


async def _default_poster(path: str, cookie: str, data: dict[str, Any]) -> Any:
    import aiohttp

    body = urllib.parse.urlencode(data)
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Cookie": cookie,
        "Referer": "https://user.qzone.qq.com/",
    }
    try:
        async with asyncio.timeout(20):
            async with aiohttp.ClientSession() as session:
                async with session.post(path, data=body, headers=headers) as response:
                    text = await response.text()
                    if response.status in (301, 302, 401, 403):
                        raise QzoneError(f"auth challenge: HTTP {response.status}")
                    if response.status != 200:
                        raise QzoneError(f"HTTP {response.status}")
    except TimeoutError as error:
        raise QzoneError("qzone request timed out") from error
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start >= 0 and "_Callback(" in text[:start]:
            inner = text[text.index("_Callback(") + 10:].rstrip(");")
            try:
                return json.loads(inner)
            except json.JSONDecodeError:
                pass
        return {"raw": text[:2000]}

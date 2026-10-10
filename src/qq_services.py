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

    async def handle_group_invite(self, flag: str, approve: bool = True, reason: str = "",
                                  sub_type: str = "invite") -> Any:
        """处理群相关申请。**sub_type 必须对**：invite=邀请你进群，add=别人申请进你的群。

        实录：这里曾把 sub_type 写死成 "invite"，于是"别人申请入群"用同一个 flag 去批，
        OneBot 侧对不上、申请就一直挂着——两类申请的处理方式必须由调用方区分。
        """
        kind = str(sub_type or "invite").strip().lower()
        if kind not in {"add", "invite"}:
            kind = "invite"
        return await self.gateway.execute(
            "set_group_add_request", flag=str(flag)[:128], sub_type=kind,
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

    async def set_profile(self, *, nickname: str | None = None, longnick: str | None = None,
                          personal_note: str | None = None, sex: int | None = None) -> Any:
        """改自己的资料。`set_qq_profile` 一次能带 nickname/personal_note/sex
        （SnowLuma catalog：nickname 选填、personal_note 选填、sex 0未知/1男/2女）；
        签名走 `set_self_longnick`（那是另一个动作）。"""
        results: list[Any] = []
        payload: dict[str, Any] = {}
        if nickname:
            payload["nickname"] = str(nickname)[:24]
        if personal_note is not None:
            payload["personal_note"] = str(personal_note)[:120]
        if sex is not None:
            try:
                payload["sex"] = max(0, min(int(sex), 2))
            except (TypeError, ValueError):
                payload["sex"] = 0
        if payload:
            results.append(await self.gateway.execute("set_qq_profile", **payload))
        if longnick is not None:
            results.append(await self.gateway.execute(
                "set_self_longnick", longNick=str(longnick)[:120]))
        return results

    async def login_info(self) -> dict[str, Any]:
        """自己的账号信息（uin/昵称）——用来拼头像地址、确认身份。"""
        payload = await self.gateway.execute("get_login_info")
        data = payload.get("data", payload) if isinstance(payload, dict) else payload
        return _json_safe(data) if isinstance(data, dict) else {}

    async def profile_like(self, user_id: str | int | None = None,
                           start: int = 0, count: int = 20) -> Any:
        """资料卡点赞情况（谁给我点过赞）。"""
        params: dict[str, Any] = {"start": int(start), "count": min(max(int(count), 1), 50)}
        if user_id:
            params["user_id"] = int(user_id)
        return _json_safe(await self.gateway.execute("get_profile_like", **params))

    @staticmethod
    def avatar_url(user_id: str | int, size: int = 640) -> str:
        """QQ 官方头像地址（没有"下载头像"的动作，但官方有固定 URL）。

        插件做聊天卡片时本来就在用 q1.qlogo.cn，这里把它固化成可复用的入口。
        """
        return f"https://q1.qlogo.cn/g?b=qq&nk={int(user_id)}&s={int(size)}"

    @staticmethod
    def group_avatar_url(group_id: str | int, size: int = 640) -> str:
        """群头像地址（同样走 q1.qlogo.cn）。"""
        return f"https://p.qlogo.cn/gh/{int(group_id)}/{int(group_id)}/{int(size)}/"

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

    async def friend_feeds(self, count: int = 10, page: int = 1) -> dict[str, Any]:
        """好友动态（`get_qzone_feeds`）：返回 feeds[]，每项含作者 uin/昵称/时间/key/html。

        注意：`key` 是 Qzone 的 feed 句柄，**不是 tid**——要对某条说说点赞/评论，
        得先 `friend_posts(uin)` 拿那条说说的 tid。这里把 html 去掉（太长），
        并把时间换算成可读文本，方便模型判断"这条是不是新的"。
        """
        payload = await self.gateway.execute(
            "get_qzone_feeds", page_num=max(1, int(page)),
            count=min(max(int(count), 1), 20))
        data = payload.get("data", payload) if isinstance(payload, dict) else payload
        if not isinstance(data, dict):
            return {"feeds": [], "has_more": False}
        feeds = []
        for item in (data.get("feeds") or []):
            if not isinstance(item, dict):
                continue
            feeds.append({
                "uin": item.get("uin"),
                "nickname": str(item.get("nickname") or "")[:40],
                "time": item.get("time"),
                "appid": item.get("appid"),
                "key": str(item.get("key") or "")[:200],
                "is_taotao": int(item.get("appid") or 0) == 311,
            })
        return {"feeds": feeds, "has_more": bool(data.get("has_more"))}

    async def friend_posts(self, uin: str | int, count: int = 10,
                           pos: int = 0) -> list[dict[str, Any]]:
        """某个好友的说说列表（`get_qzone_msg_list(target_uin=…)`）——拿 tid 用。"""
        payload = await self.gateway.execute(
            "get_qzone_msg_list", target_uin=int(uin),
            pos=max(0, int(pos)), num=min(max(int(count), 1), 20))
        data = payload.get("data", payload) if isinstance(payload, dict) else payload
        rows = data.get("msglist") if isinstance(data, dict) else None
        posts: list[dict[str, Any]] = []
        for item in (rows or []):
            if not isinstance(item, dict):
                continue
            posts.append({
                "tid": str(item.get("tid") or ""),
                "content": str(item.get("content") or "")[:200],
                "created_time": item.get("created_time"),
                "comment_count": item.get("comment_count"),
                "like_count": item.get("like_count"),
                "uin": uin,
            })
        return posts

    async def like_friend_post(self, uin: str | int, tid: str,
                               created_time: Any = None) -> Any:
        """给**好友**的说说点赞（要 tid + target_uin + abstime）。"""
        params: dict[str, Any] = {"tid": str(tid), "target_uin": int(uin)}
        try:
            if created_time is not None:
                params["abstime"] = int(created_time)
        except (TypeError, ValueError):
            pass
        return _json_safe(await self.gateway.execute("like_qzone", **params))

    async def comment_friend_post(self, uin: str | int, tid: str, content: str) -> Any:
        """评论**好友**的说说（要 tid + target_uin + content）。"""
        text = str(content or "").strip()
        if not text or len(text) > 500:
            raise QzoneError("评论要 1-500 字")
        return _json_safe(await self.gateway.execute(
            "comment_qzone", tid=str(tid), content=text, target_uin=int(uin)))

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

    # 发说说的去重/冷却状态（历史落盘：重启也不忘，否则重启后又发一遍同样的）
    _publish_history: "list[tuple[float, str]]" = []
    _PUBLISH_COOLDOWN_SECONDS = 900.0      # 15 分钟内不再发第二条
    _PUBLISH_SIMILAR_RATIO = 0.72          # 与近期相似度超过这个就不发
    _PUBLISH_WINDOW_SECONDS = 48 * 3600.0  # 去重与话题统计都看最近 48 小时
    _PUBLISH_TOPIC_HITS = 2                # 同一个词在近 8 条里出现 ≥2 次 = 同一话题
    _PUBLISH_DAILY_CAP = 8                 # 24 小时最多发几条（真人也就这个量级）
    _history_path: "str | None" = None

    # 常见虚词/时间词的 2-gram：它们重合不代表同话题（"今天""就是"谁都天天说）
    _STOP_BIGRAMS = frozenset({
        "今天", "昨天", "明天", "现在", "然后", "但是", "可是", "就是", "还是",
        "真的", "感觉", "有点", "一下", "自己", "我们", "你们", "他们", "这个",
        "那个", "什么", "怎么", "可以", "已经", "应该", "不要", "没有", "一个",
        "时候", "因为", "所以", "如果", "虽然", "而且", "起来", "过来", "出来",
        "这样", "那样", "再来", "再去", "想去", "要去", "好想", "真是", "突然",
        "终于", "其实", "只是", "不过", "不如", "不如", "下次", "这次", "这次",
    })

    @classmethod
    def set_history_path(cls, path: "str | None") -> None:
        """把发布历史落盘到文件（main 启动时传入插件数据目录）。"""
        cls._history_path = str(path) if path else None
        cls._publish_history = cls._load_history()

    @classmethod
    def _load_history(cls) -> "list[tuple[float, str]]":
        import json as _json

        if not cls._history_path:
            return list(cls._publish_history)
        try:
            with open(cls._history_path, "r", encoding="utf-8") as handle:
                rows = _json.load(handle)
            out = []
            for row in rows if isinstance(rows, list) else []:
                if isinstance(row, (list, tuple)) and len(row) >= 2:
                    out.append((float(row[0]), str(row[1])))
            return out[-64:]
        except Exception:
            return list(cls._publish_history)

    @classmethod
    def _save_history(cls) -> None:
        import json as _json

        if not cls._history_path:
            return
        try:
            with open(cls._history_path, "w", encoding="utf-8") as handle:
                _json.dump(cls._publish_history[-64:], handle, ensure_ascii=False)
        except Exception:
            pass

    @staticmethod
    def _content_bigrams(text: str) -> "set[str]":
        """说说里的"内容词"碎片（2-gram，去掉虚词/标点/空白/纯字母数字）。

        用来做**话题级**判断："松饼要热的 唱两句再说"和"松饼还没吃够"措辞不同，
        但"松饼"这个碎片是同一个——同话题换说法就靠它抓。
        """
        cleaned = "".join(
            char for char in str(text or "")
            if char not in "，。！？、；：~～（）()【】[]「」…—\n\r\t 0123456789"
            and not ("a" <= char.lower() <= "z"))
        grams = {cleaned[i:i + 2] for i in range(len(cleaned) - 1)}
        return {gram for gram in grams if gram not in QzoneService._STOP_BIGRAMS}

    @staticmethod
    def _similarity(left: str, right: str) -> float:
        """两段文字的相似度（按 2-gram 重合率，中文短文本够用且无依赖）。"""
        a, b = str(left or ""), str(right or "")
        if not a or not b:
            return 0.0
        if a == b:
            return 1.0
        grams_a = {a[i:i + 2] for i in range(len(a) - 1)} or {a}
        grams_b = {b[i:i + 2] for i in range(len(b) - 1)} or {b}
        if not grams_a or not grams_b:
            return 0.0
        return len(grams_a & grams_b) / len(grams_a | grams_b)

    def _publish_blocker(self, text: str) -> str:
        """该不该拦这条说说？返回空串=可以发，否则一句人话原因。

        实录（用户）："bot 会把一件事反复发几次空间"——同一件事被它当成新灵感反复发；
        v1.0.11 又发现"同话题换说法"也躲过了旧守卫（"松饼要热的 唱两句再说" vs
        "松饼还没吃够"，2-gram 重合率才 0.3，但话题是同一个）。真人不会这样发。
        """
        now = time.time()          # 墙钟：历史要落盘跨重启，monotonic 重启后失义
        history = [
            (stamp, body) for stamp, body in QzoneService._publish_history
            if now - stamp < QzoneService._PUBLISH_WINDOW_SECONDS
        ]
        QzoneService._publish_history = history
        recent = history[-10:]

        # ① 近似重复（措辞像的）
        for _stamp, previous in recent:
            if self._similarity(text, previous) >= self._PUBLISH_SIMILAR_RATIO:
                return ("这件事你刚发过一条很像的说说，别再重复发了——"
                        "想发就换个别的、真的新的内容，或者干脆这次不发。")

        # ② 话题级重复（措辞不同但讲的是同一件事）
        grams = self._content_bigrams(text)
        if grams:
            best_word, best_hits = "", 0
            for gram in grams:
                hits = sum(1 for _stamp, previous in history[-8:]
                           if gram in previous)
                if hits > best_hits:
                    best_word, best_hits = gram, hits
            if best_hits >= QzoneService._PUBLISH_TOPIC_HITS:
                return (f"「{best_word}」这个话题你最近已经发过 {best_hits} 条说说了，"
                        "别再围着同一件事转——要么聊点别的，要么这次不发。")

        # ③ 每日上限（真人一天也就几条）
        day_count = sum(1 for stamp, _body in history if now - stamp < 86400)
        if day_count >= QzoneService._PUBLISH_DAILY_CAP:
            return (f"你今天已经发过 {day_count} 条说说了，歇歇吧——"
                    "明天再发也一样，别刷屏。")

        # ④ 冷却
        if history:
            waited = now - history[-1][0]
            left = self._PUBLISH_COOLDOWN_SECONDS - waited
            if left > 0:
                return (f"你 {int(waited // 60)} 分钟前刚发过说说，"
                        f"再等 {max(1, int(left // 60))} 分钟（或换个真正新的内容）。")
        return ""

    async def publish(self, text: str, images: list[str] | None = None) -> dict[str, Any]:
        text = str(text).strip()
        if not text or len(text) > 1000:
            raise QzoneError("publish text must be 1-1000 characters")
        blocker = self._publish_blocker(text)
        if blocker:
            raise QzoneError(blocker)
        try:
            params: dict[str, Any] = {"content": text}
            if images:
                params["images"] = [str(x)[:500] for x in images[:9]]
            result = await self.gateway.execute("send_qzone_msg", **params)
            QzoneService._publish_history.append((time.time(), text))
            QzoneService._publish_history = QzoneService._publish_history[-64:]
            QzoneService._save_history()
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

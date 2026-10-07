# -*- coding: utf-8 -*-
"""SnowLuma / OneBot v11 全量动作表（由 tools/gen_qq_actions.py 生成，勿手改）。

来源：SnowLuma 文档站 catalog.json，共 193 个动作。
每个动作带：分类、中文说明、只读标记、风险级别、参数表（含类型/必填/默认/说明）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


@dataclass(frozen=True, slots=True)
class QQParam:
    """一个动作参数（类型用于入参强制转换，role 用于网关侧的安全归类）。"""

    name: str
    type: str
    required: bool = False
    desc: str = ""
    default: Any = None
    role: str = ""


@dataclass(frozen=True, slots=True)
class QQAction:
    """一个 OneBot/SnowLuma 动作的完整描述。"""

    name: str
    summary: str
    category: str
    read_only: bool
    risk: str
    params: tuple[QQParam, ...] = ()
    aliases: tuple[str, ...] = ()
    returns: str = ""
    invariants: tuple[str, ...] = ()
    accepts_extra: bool = True

    @property
    def required(self) -> tuple[str, ...]:
        return tuple(p.name for p in self.params if p.required)

    def param(self, name: str) -> QQParam | None:
        for item in self.params:
            if item.name == name:
                return item
        return None


CATEGORIES: tuple[str, ...] = (
    '信息',
    '消息',
    '好友',
    '群信息',
    '群管理',
    '群文件',
    '请求',
    '扩展',
    '群相册',
    '空间',
    '系统表情',
    '流式接口',
)

ACTIONS: dict[str, QQAction] = {}


def _register(action: QQAction) -> None:
    ACTIONS[action.name] = action


_register(QQAction(
    name='can_send_image',
    summary='',
    category='信息',
    read_only=True,
    risk='read',
    returns='能力查询结果。',
))
_register(QQAction(
    name='can_send_record',
    summary='',
    category='信息',
    read_only=True,
    risk='read',
    returns='能力查询结果。',
))
_register(QQAction(
    name='get_login_info',
    summary='',
    category='信息',
    read_only=True,
    risk='read',
    returns='当前登录账号的 QQ 号与昵称。',
))
_register(QQAction(
    name='get_status',
    summary='',
    category='信息',
    read_only=True,
    risk='read',
    returns='运行状态。`online` 表示账号在线；`good` 表示已确认的收发链路健康状态。',
))
_register(QQAction(
    name='get_version_info',
    summary='',
    category='信息',
    read_only=True,
    risk='read',
    returns='实现与协议版本信息。',
))
_register(QQAction(
    name='delete_friend',
    summary='删除好友',
    category='好友',
    read_only=False,
    risk='destructive',
    params=(
        QQParam(name='user_id', type='uint', required=True, desc='QQ 号', default=None, role='user_id'),
        QQParam(name='block', type='bool', required=False, desc='', default=False, role=''),
    ),
))
_register(QQAction(
    name='get_friend_list',
    summary='获取好友列表',
    category='好友',
    read_only=True,
    risk='read',
    returns='好友列表数组，每项含 QQ 号、昵称与备注。',
))
_register(QQAction(
    name='get_stranger_info',
    summary='获取陌生人信息',
    category='好友',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='user_id', type='uint', required=True, desc='QQ 号', default=None, role='user_id'),
    ),
    returns='用户资料：QQ 号、昵称、好友备注、性别、年龄与个性签名，命中资料时另含等级、企点标志与企业名称。',
))
_register(QQAction(
    name='.get_word_slices',
    summary='分词（未实现）',
    category='扩展',
    read_only=True,
    risk='read',
))
_register(QQAction(
    name='_del_group_notice',
    summary='删除群公告（fid 或 notice_id 二选一）',
    category='扩展',
    read_only=False,
    risk='destructive',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='fid', type='string', required=False, desc='', default=None, role=''),
        QQParam(name='notice_id', type='string', required=False, desc='', default=None, role=''),
    ),
))
_register(QQAction(
    name='_get_friend_dress',
    summary='获取指定 QQ 号正在使用的个性装扮（挂件/名片/来电/输入状态等）',
    category='扩展',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='user_id', type='uint', required=True, desc='目标 QQ 号', default=None, role='user_id'),
    ),
    returns='装扮信息；目标未使用任何可查询装扮时 items 为空数组。网络失败、未登录态/风控、页面改版、返回账号与请求不一致时返回失败并附具体原因',
))
_register(QQAction(
    name='_get_group_notice',
    summary='获取群公告',
    category='扩展',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
    ),
    returns='普通公告与新成员公告的合并数组；send_to_new_members 标识后者',
))
_register(QQAction(
    name='_get_model_show',
    summary='获取机型展示（兼容 mock）',
    category='扩展',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='model', type='string', required=False, desc='', default='', role=''),
    ),
    returns='数组，每项含 variants（回显请求的机型名与 need_pay 标记）。',
))
_register(QQAction(
    name='_mark_all_as_read',
    summary='标记全部已读',
    category='扩展',
    read_only=False,
    risk='send',
))
_register(QQAction(
    name='_send_group_notice',
    summary='发送群公告（支持置顶、弹窗、新成员、群昵称引导与确认）',
    category='扩展',
    read_only=False,
    risk='social',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='content', type='string', required=True, desc='公告正文', default=None, role=''),
        QQParam(name='image', type='string', required=False, desc='可选公告图片（本地路径、URL 或 base64）', default='', role='image'),
        QQParam(name='pinned', type='int', required=False, desc='是否置顶：1=置顶，0=不置顶', default=0, role=''),
        QQParam(name='type', type='int', required=False, desc='发布类型：1=普通公告，20=新成员公告；建议使用 send_to_new_members', default=None, role=''),
        QQParam(name='send_to_new_members', type='bool', required=False, desc='是否在成员新加入群时发送（与 type=20 等价）', default=None, role=''),
        QQParam(name='is_show_edit_card', type='int', required=False, desc='是否引导群成员修改群昵称：1=是，0=否', default=1, role=''),
        QQParam(name='tip_window_type', type='int', required=False, desc='弹窗展示：0=开启弹窗，1=关闭弹窗（QQ 原始字段为反向语义）', default=1, role=''),
        QQParam(name='confirm_required', type='int', required=False, desc='是否需要群成员确认收到：1=是，0=否', default=1, role=''),
    ),
    invariants=('type must be 1 (regular) or 20 (new members)', 'send_to_new_members conflicts with type',),
))
_register(QQAction(
    name='_set_model_show',
    summary='设置机型展示（占位）',
    category='扩展',
    read_only=False,
    risk='destructive',
))
_register(QQAction(
    name='add_custom_face',
    summary='添加收藏表情',
    category='扩展',
    read_only=False,
    risk='social',
    params=(
        QQParam(name='file', type='string', required=True, desc='', default=None, role='image'),
    ),
))
_register(QQAction(
    name='bot_exit',
    summary='退出机器人',
    category='扩展',
    read_only=False,
    risk='destructive',
))
_register(QQAction(
    name='cancel_group_todo',
    summary='取消群待办',
    category='扩展',
    read_only=False,
    risk='social',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='message_id', type='messageId', required=True, desc='', default=None, role='message_id'),
    ),
))
_register(QQAction(
    name='check_url_safely',
    summary='检查链接安全性',
    category='扩展',
    read_only=True,
    risk='read',
    returns='{ level }：安全等级（占位实现，恒为 1）。',
))
_register(QQAction(
    name='clean_cache',
    summary='清理缓存',
    category='扩展',
    read_only=False,
    risk='destructive',
))
_register(QQAction(
    name='click_inline_keyboard_button',
    summary='点击内联键盘按钮',
    category='扩展',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='bot_appid', type='uint', required=True, desc='', default=None, role=''),
        QQParam(name='msg_seq', type='uint', required=True, desc='', default=None, role=''),
        QQParam(name='button_id', type='string', required=True, desc='', default=None, role=''),
        QQParam(name='callback_data', type='string', required=False, desc='', default='', role=''),
    ),
))
_register(QQAction(
    name='complete_group_todo',
    summary='完成群待办',
    category='扩展',
    read_only=False,
    risk='social',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='message_id', type='messageId', required=True, desc='', default=None, role='message_id'),
    ),
))
_register(QQAction(
    name='create_collection',
    summary='创建收藏（未实现）',
    category='扩展',
    read_only=False,
    risk='send',
))
_register(QQAction(
    name='create_flash_task',
    summary='创建闪传任务',
    category='扩展',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='files', type='raw', required=False, desc='路径、{ file, name }，或它们的数组', default=None, role=''),
        QQParam(name='name', type='string', required=False, desc='', default=None, role=''),
        QQParam(name='thumb_path', type='string', required=False, desc='', default=None, role=''),
    ),
))
_register(QQAction(
    name='delete_custom_face',
    summary='删除收藏表情',
    category='扩展',
    read_only=False,
    risk='destructive',
    params=(
        QQParam(name='emoji_id', type='string', required=True, desc='', default=None, role=''),
    ),
))
_register(QQAction(
    name='delete_essence_msg',
    summary='移除精华消息',
    category='扩展',
    read_only=False,
    risk='destructive',
    params=(
        QQParam(name='message_id', type='messageId', required=True, desc='', default=None, role='message_id'),
    ),
))
_register(QQAction(
    name='delete_flash_file',
    summary='删除闪传文件',
    category='扩展',
    read_only=False,
    risk='destructive',
    params=(
        QQParam(name='fileset_id', type='string', required=True, desc='', default=None, role=''),
    ),
))
_register(QQAction(
    name='delete_group_folder',
    summary='删除群文件夹',
    category='扩展',
    read_only=False,
    risk='destructive',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='folder_id', type='string', required=True, desc='', default=None, role=''),
    ),
))
_register(QQAction(
    name='download_file',
    summary='下载文件（url 或 base64）到 data/downloads',
    category='扩展',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='url', type='string', required=False, desc='', default='', role=''),
        QQParam(name='base64', type='string', required=False, desc='', default='', role=''),
        QQParam(name='name', type='string', required=False, desc='', default='', role=''),
        QQParam(name='headers', type='raw', required=False, desc='', default=None, role=''),
    ),
    returns='{ file }',
))
_register(QQAction(
    name='download_fileset',
    summary='解析闪传文件下载直链（不下载，由调用方实现下载）',
    category='扩展',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='fileset_id', type='string', required=True, desc='', default=None, role=''),
        QQParam(name='file_name', type='string', required=False, desc='', default=None, role=''),
        QQParam(name='file_index', type='number', required=False, desc='', default=None, role=''),
    ),
    returns='{ url, file_name, file_size }',
))
_register(QQAction(
    name='fetch_custom_face',
    summary='获取自定义表情',
    category='扩展',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='count', type='int', required=False, desc='', default=10, role=''),
        QQParam(name='return_type', type='string', required=False, desc='', default='url', role=''),
    ),
    returns='字符串数组：return_type=url 时为图片 URL，return_type=id 时为 emoji_id。',
))
_register(QQAction(
    name='fetch_custom_face_detail',
    summary='获取自定义表情详情',
    category='扩展',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='count', type='int', required=False, desc='', default=48, role=''),
    ),
    returns='自定义表情详情数组，包含资源标识、图片地址、摘要与描述。',
))
_register(QQAction(
    name='fetch_emoji_like',
    summary='获取表情回应用户（NapCat 分页）',
    category='扩展',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='message_id', type='messageId', required=True, desc='', default=None, role='message_id'),
        QQParam(name='emojiId', type='string', required=True, desc='', default=None, role=''),
        QQParam(name='count', type='int', required=False, desc='', default=10, role=''),
        QQParam(name='cookie', type='string', required=False, desc='', default='', role=''),
    ),
    returns='分页的表情回应用户列表（NapCat 形状），含分页游标 cookie 与首/末页标记。',
))
_register(QQAction(
    name='fetch_ptt_text',
    summary='获取语音转文字结果',
    category='扩展',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='message_id', type='messageId', required=True, desc='', default=None, role='message_id'),
    ),
    aliases=('get_ptt_text', 'get_record_text',),
    returns='{ text }：语音识别出的文本。',
))
_register(QQAction(
    name='forward_friend_single_msg',
    summary='转发单条消息给好友',
    category='扩展',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='message_id', type='messageId', required=True, desc='', default=None, role='message_id'),
        QQParam(name='user_id', type='uint', required=True, desc='', default=None, role='user_id'),
    ),
))
_register(QQAction(
    name='forward_group_single_msg',
    summary='转发单条消息到群',
    category='扩展',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='message_id', type='messageId', required=True, desc='', default=None, role='message_id'),
        QQParam(name='group_id', type='uint', required=True, desc='', default=None, role='group_id'),
    ),
))
_register(QQAction(
    name='friend_poke',
    summary='好友拍一拍',
    category='扩展',
    read_only=False,
    risk='social',
    params=(
        QQParam(name='user_id', type='uint', required=True, desc='', default=None, role='user_id'),
        QQParam(name='target_id', type='uint', required=False, desc='', default=None, role='user_id'),
    ),
))
_register(QQAction(
    name='get_ai_characters',
    summary='获取 AI 语音角色',
    category='扩展',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='chat_type', type='int', required=False, desc='', default=1, role=''),
    ),
    returns='按分类分组的 AI 语音角色列表，每组含分类名与角色（id、名称、试听 URL）。',
))
_register(QQAction(
    name='get_ai_record',
    summary='生成 AI 语音',
    category='扩展',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='character', type='string', required=True, desc='', default=None, role=''),
        QQParam(name='text', type='string', required=True, desc='', default=None, role=''),
        QQParam(name='chat_type', type='int', required=False, desc='', default=1, role=''),
    ),
))
_register(QQAction(
    name='get_clientkey',
    summary='获取 clientkey',
    category='扩展',
    read_only=True,
    risk='credential',
    returns='{ clientKey, expireTime, keyIndex }：clientkey 及其过期时间与索引。',
))
_register(QQAction(
    name='get_collection_list',
    summary='获取收藏列表',
    category='扩展',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='category', type='int', required=False, desc='收藏分类 ID；0 表示全部分类', default=0, role=''),
        QQParam(name='count', type='int', required=False, desc='最多返回的收藏数量', default=50, role=''),
    ),
    returns='返回收藏条目、是否还有更多数据及底部时间游标。',
))
_register(QQAction(
    name='get_cookies',
    summary='获取 Cookies',
    category='扩展',
    read_only=True,
    risk='credential',
    params=(
        QQParam(name='domain', type='string', required=False, desc='', default='qun.qq.com', role=''),
    ),
    returns='{ cookies }：指定域名的 Cookie 字符串。',
))
_register(QQAction(
    name='get_credentials',
    summary='获取凭证',
    category='扩展',
    read_only=True,
    risk='credential',
    params=(
        QQParam(name='domain', type='string', required=False, desc='', default='qun.qq.com', role=''),
    ),
    returns='{ cookies, token, csrf_token }：Cookie 字符串与 CSRF 令牌（token 与 csrf_token 同值）。',
))
_register(QQAction(
    name='get_csrf_token',
    summary='获取 CSRF 令牌',
    category='扩展',
    read_only=True,
    risk='credential',
    returns='{ token }：CSRF 令牌（bkn，数值）。',
))
_register(QQAction(
    name='get_doubt_friends_add_request',
    summary='获取可疑好友申请',
    category='扩展',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='count', type='int', required=False, desc='', default=50, role=''),
    ),
    returns='可疑好友申请数组，每项含 uid（作为处理用 flag）、昵称、来源、留言与申请时间。',
))
_register(QQAction(
    name='get_emoji_likes',
    summary='获取表情回应用户',
    category='扩展',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='message_id', type='messageId', required=True, desc='', default=None, role='message_id'),
        QQParam(name='emoji_id', type='string', required=True, desc='', default=None, role=''),
    ),
    returns='{ emoji_like_list }：回应该表情的用户列表（nick_name 恒为空串）。',
))
_register(QQAction(
    name='get_essence_msg_list',
    summary='获取精华消息列表',
    category='扩展',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
    ),
))
_register(QQAction(
    name='get_file',
    summary='获取文件信息（仅图片/语音缓存；群文件请用 get_group_file_url）',
    category='扩展',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='file_id', type='string', required=False, desc='', default='', role='file_id'),
        QQParam(name='file', type='string', required=False, desc='', default='', role='file'),
    ),
))
_register(QQAction(
    name='get_fileset_id',
    summary='从 QQ 闪传分享码或官方分享链接获取 fileset_id',
    category='扩展',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='share_code', type='string', required=True, desc='QQ 闪传分享码，或 https://qfile.qq.com/q/... 官方分享链接', default=None, role=''),
    ),
    returns='{ fileset_id }：解析出的文件集 ID。',
))
_register(QQAction(
    name='get_fileset_info',
    summary='获取文件集信息',
    category='扩展',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='fileset_id', type='string', required=True, desc='', default=None, role=''),
    ),
))
_register(QQAction(
    name='get_flash_file_list',
    summary='获取闪传文件列表',
    category='扩展',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='fileset_id', type='string', required=True, desc='', default=None, role=''),
    ),
))
_register(QQAction(
    name='get_flash_file_url',
    summary='获取闪传文件链接',
    category='扩展',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='fileset_id', type='string', required=True, desc='', default=None, role=''),
        QQParam(name='file_name', type='string', required=False, desc='', default=None, role=''),
        QQParam(name='file_index', type='number', required=False, desc='', default=None, role=''),
    ),
))
_register(QQAction(
    name='get_forward_msg',
    summary='获取合并转发消息（id 或 message_id）',
    category='扩展',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='id', type='string', required=False, desc='', default=None, role=''),
        QQParam(name='message_id', type='string', required=False, desc='', default=None, role=''),
    ),
    returns='{ messages }：转发内的消息节点数组（每项为 OneBot 消息事件，内部字段不固定）。',
))
_register(QQAction(
    name='get_friend_msg_history',
    summary='获取好友消息历史（无锚点时从服务器获取最新双向记录）',
    category='扩展',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='user_id', type='uint', required=True, desc='', default=None, role='user_id'),
        QQParam(name='message_id', type='int', required=False, desc='', default=0, role='message_id'),
        QQParam(name='count', type='int', required=False, desc='无锚点时从 QQ 服务器获取最新双向历史；服务器或身份解析失败时动作失败，不返回不完整的本地缓存', default=20, role=''),
        QQParam(name='reverse_order', type='bool', required=False, desc='仅在 message_id 非 0 时生效；true 返回锚点及更旧消息，false 返回锚点及更新消息', default=True, role=''),
    ),
    returns='{ messages }：好友消息事件对象数组（每项为 OneBot 消息事件，内部字段不固定）。',
))
_register(QQAction(
    name='get_friends_with_category',
    summary='获取分组好友列表',
    category='扩展',
    read_only=True,
    risk='read',
    returns='好友分组数组；每组包含 categoryId、categoryName、categoryMbCount 和 buddyList。',
))
_register(QQAction(
    name='get_group_at_all_remain',
    summary='获取群 @全体成员 剩余次数',
    category='扩展',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
    ),
    returns='{ can_at_all, remain_at_all_count_for_group, remain_at_all_count_for_uin }：@全体可用性与剩余次数。',
))
_register(QQAction(
    name='get_group_detail_info',
    summary='获取群详细信息',
    category='扩展',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='', default=None, role='group_id'),
        QQParam(name='no_cache', type='bool', required=False, desc='', default=False, role=''),
    ),
))
_register(QQAction(
    name='get_group_file_system_info',
    summary='获取群文件系统信息',
    category='扩展',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
    ),
    returns='{ file_count, limit_count, used_space, total_space }：群文件数量与容量信息（均为服务端实际值）。',
))
_register(QQAction(
    name='get_group_ignore_add_request',
    summary='获取被忽略的入群请求（NapCat）',
    category='扩展',
    read_only=True,
    risk='read',
    returns='被忽略的入群请求数组（NapCat 字段命名），每项含请求序列、邀请人、群信息与处理标记。',
))
_register(QQAction(
    name='get_group_ignored_notifies',
    summary='获取被过滤的入群请求',
    category='扩展',
    read_only=True,
    risk='read',
    returns='被过滤的入群请求数组，每项含群号、申请人、邀请人、留言与处理标记。',
))
_register(QQAction(
    name='get_group_info_ex',
    summary='获取群信息（扩展）',
    category='扩展',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='', default=None, role='group_id'),
        QQParam(name='no_cache', type='bool', required=False, desc='', default=False, role=''),
    ),
))
_register(QQAction(
    name='get_group_msg_history',
    summary='获取群消息历史',
    category='扩展',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='message_id', type='int', required=False, desc='', default=0, role='message_id'),
        QQParam(name='count', type='int', required=False, desc='', default=20, role=''),
        QQParam(name='reverse_order', type='bool', required=False, desc='仅在 message_id 非 0 时生效；true 返回锚点及更旧消息，false 返回锚点及更新消息', default=True, role=''),
    ),
    returns='{ messages }：群消息事件对象数组（每项为 OneBot 消息事件，内部字段不固定）。',
))
_register(QQAction(
    name='get_group_shut_list',
    summary='获取群禁言列表',
    category='扩展',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
    ),
    returns='仍在禁言中的成员数组，每项含 QQ 号、昵称与禁言到期时间戳（秒）。',
))
_register(QQAction(
    name='get_group_signed_list',
    summary='获取群今日打卡列表',
    category='扩展',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
    ),
))
_register(QQAction(
    name='get_group_todo_list',
    summary='获取群待办列表',
    category='扩展',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
    ),
    returns='群待办数组，包含可用于待办操作的消息标识、服务端摘要与时间',
))
_register(QQAction(
    name='get_image',
    summary='获取图片信息',
    category='扩展',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='file', type='string', required=False, desc='', default='', role='image'),
        QQParam(name='file_id', type='string', required=False, desc='', default='', role='file_id'),
    ),
))
_register(QQAction(
    name='get_mini_app_ark',
    summary='获取小程序卡片 ark',
    category='扩展',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='type', type='string', required=False, desc='', default=None, role=''),
        QQParam(name='title', type='string', required=False, desc='', default=None, role=''),
        QQParam(name='desc', type='string', required=False, desc='', default=None, role=''),
        QQParam(name='picUrl', type='string', required=False, desc='', default=None, role=''),
        QQParam(name='pic_url', type='string', required=False, desc='', default=None, role=''),
        QQParam(name='jumpUrl', type='string', required=False, desc='', default=None, role=''),
        QQParam(name='jump_url', type='string', required=False, desc='', default=None, role=''),
    ),
))
_register(QQAction(
    name='get_msg_emoji_likes',
    summary='获取一条消息的全部表情回应',
    category='扩展',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='message_id', type='messageId', required=True, desc='', default=None, role='message_id'),
    ),
    returns='该消息上每个表情回应的编号、数量与用户列表。',
))
_register(QQAction(
    name='get_online_clients',
    summary='获取在线客户端',
    category='扩展',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='no_cache', type='bool', required=False, desc='是否强制刷新；QQ 当前仅暴露本地快照，true 会明确失败', default=False, role=''),
    ),
    returns='{ clients }：QQ 本次会话最近推送的在线设备快照。',
))
_register(QQAction(
    name='get_profile_like',
    summary='获取资料点赞',
    category='扩展',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='user_id', type='int', required=False, desc='', default=0, role='user_id'),
        QQParam(name='start', type='int', required=False, desc='', default=0, role=''),
        QQParam(name='count', type='int', required=False, desc='', default=10, role=''),
    ),
    returns='点赞资料：uid、最近点赞时间、收藏与点赞统计及用户明细。',
))
_register(QQAction(
    name='get_recent_contact',
    summary='获取最近会话（占位）',
    category='扩展',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='count', type='int', required=False, desc='', default=10, role=''),
    ),
    returns='占位实现，恒返回空数组。',
))
_register(QQAction(
    name='get_record',
    summary='获取语音信息；传 out_format 则服务端转码并附带 base64',
    category='扩展',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='file', type='string', required=False, desc='', default='', role='record'),
        QQParam(name='file_id', type='string', required=False, desc='', default='', role='file_id'),
        QQParam(name='out_format', type='enum', required=False, desc='', default=None, role=''),
    ),
))
_register(QQAction(
    name='get_rkey',
    summary='获取下载 rkey',
    category='扩展',
    read_only=True,
    risk='credential',
    aliases=('nc_get_rkey',),
))
_register(QQAction(
    name='get_rkey_server',
    summary='获取 rkey 服务器信息',
    category='扩展',
    read_only=True,
    risk='credential',
    returns='{ expired_time, name, private_rkey?, group_rkey? }：rkey 过期时间与（存在时的）私聊/群聊 rkey。',
))
_register(QQAction(
    name='get_share_link',
    summary='获取文件分享链接',
    category='扩展',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='fileset_id', type='string', required=True, desc='', default=None, role=''),
    ),
))
_register(QQAction(
    name='get_unidirectional_friend_list',
    summary='获取单向好友列表',
    category='扩展',
    read_only=True,
    risk='read',
))
_register(QQAction(
    name='group_poke',
    summary='群拍一拍',
    category='扩展',
    read_only=False,
    risk='social',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='user_id', type='uint', required=True, desc='QQ 号', default=None, role='member_id'),
    ),
))
_register(QQAction(
    name='list_filesets',
    summary='列出当前账号的所有闪传文件集',
    category='扩展',
    read_only=False,
    risk='send',
))
_register(QQAction(
    name='mark_group_msg_as_read',
    summary='标记群消息已读',
    category='扩展',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='message_id', type='messageId', required=True, desc='', default=None, role='message_id'),
        QQParam(name='group_id', type='uint', required=False, desc='', default=None, role='group_id'),
    ),
))
_register(QQAction(
    name='mark_msg_as_read',
    summary='标记消息已读（群聊/私聊自动路由）',
    category='扩展',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='message_id', type='messageId', required=True, desc='', default=None, role='message_id'),
        QQParam(name='target_id', type='uint', required=False, desc='', default=None, role=''),
    ),
))
_register(QQAction(
    name='mark_private_msg_as_read',
    summary='标记私聊消息已读',
    category='扩展',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='message_id', type='messageId', required=True, desc='', default=None, role='message_id'),
        QQParam(name='user_id', type='uint', required=False, desc='', default=None, role='user_id'),
    ),
))
_register(QQAction(
    name='modify_custom_face',
    summary='修改收藏表情备注',
    category='扩展',
    read_only=False,
    risk='social',
    params=(
        QQParam(name='emoji_id', type='string', required=True, desc='', default=None, role=''),
        QQParam(name='desc', type='string', required=False, desc='', default='', role=''),
    ),
))
_register(QQAction(
    name='move_custom_face_to_front',
    summary='收藏表情移到最前',
    category='扩展',
    read_only=False,
    risk='social',
    params=(
        QQParam(name='emoji_id', type='string', required=True, desc='', default=None, role=''),
    ),
))
_register(QQAction(
    name='nc_get_packet_status',
    summary='获取 packet 状态（占位）',
    category='扩展',
    read_only=True,
    risk='read',
    returns='占位实现，恒返回 null。',
))
_register(QQAction(
    name='nc_get_user_status',
    summary='获取用户在线/扩展状态',
    category='扩展',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='user_id', type='uint', required=True, desc='', default=None, role='user_id'),
    ),
    returns='{ status, ext_status }：用户在线状态码与扩展状态码。',
))
_register(QQAction(
    name='ocr_image',
    summary='OCR 图片（服务端，需图片 URL 或已缓存的图片 file_id）',
    category='扩展',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='image', type='string', required=True, desc='', default=None, role='image'),
    ),
    aliases=('.ocr_image',),
    returns='{ texts, language }：识别文本数组（含置信度与坐标）与识别语言。',
))
_register(QQAction(
    name='rename_flash_file',
    summary='重命名闪传文件',
    category='扩展',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='fileset_id', type='string', required=True, desc='', default=None, role=''),
        QQParam(name='new_name', type='string', required=True, desc='', default=None, role=''),
    ),
))
_register(QQAction(
    name='request_decrypt_key',
    summary='请求数据库解密密钥',
    category='扩展',
    read_only=False,
    risk='credential',
    params=(
        QQParam(name='db_path', type='string', required=True, desc='', default=None, role=''),
    ),
))
_register(QQAction(
    name='send_ark_share',
    summary='分享用户/群 Ark 卡片（NapCat 标准名）',
    category='扩展',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='user_id', type='uint', required=False, desc='', default=None, role='user_id'),
        QQParam(name='group_id', type='uint', required=False, desc='', default=None, role='group_id'),
        QQParam(name='phone_number', type='string', required=False, desc='', default='', role=''),
    ),
    returns='{ arkMsg }：服务端生成的推荐联系人 Ark 卡片 JSON 字符串。',
))
_register(QQAction(
    name='send_flash_msg',
    summary='发送闪传消息（私聊或群聊，引用 fileset_id 让对端下载）',
    category='扩展',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='fileset_id', type='string', required=True, desc='', default=None, role=''),
        QQParam(name='user_id', type='uint', required=False, desc='', default=None, role='user_id'),
        QQParam(name='group_id', type='uint', required=False, desc='', default=None, role='group_id'),
    ),
    returns='{ message_id }',
))
_register(QQAction(
    name='send_forward_msg',
    summary='发送合并转发（按 message_type/群号自动路由）',
    category='扩展',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='messages', type='message', required=False, desc='', default=None, role=''),
        QQParam(name='message', type='message', required=False, desc='', default=None, role=''),
        QQParam(name='message_type', type='string', required=False, desc='', default=None, role=''),
        QQParam(name='group_id', type='int', required=False, desc='', default=None, role=''),
        QQParam(name='user_id', type='int', required=False, desc='', default=None, role=''),
        QQParam(name='source', type='string', required=False, desc='', default=None, role=''),
        QQParam(name='summary', type='string', required=False, desc='', default=None, role=''),
        QQParam(name='prompt', type='string', required=False, desc='', default=None, role=''),
        QQParam(name='news', type='raw', required=False, desc='', default=None, role=''),
    ),
    returns='{ message_id, res_id, forward_id }',
))
_register(QQAction(
    name='send_group_ai_record',
    summary='发送 AI 语音到群',
    category='扩展',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='character', type='string', required=True, desc='', default=None, role=''),
        QQParam(name='text', type='string', required=True, desc='', default=None, role=''),
        QQParam(name='chat_type', type='int', required=False, desc='', default=1, role=''),
    ),
    returns='{ message_id }：已发送 AI 语音的 OneBot 消息 ID。',
))
_register(QQAction(
    name='send_group_ark_share',
    summary='分享群 Ark 卡片（NapCat 标准名）',
    category='扩展',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='', default=None, role='group_id'),
    ),
    returns='服务端生成的群推荐 Ark 卡片 JSON 字符串。',
))
_register(QQAction(
    name='send_group_forward_msg',
    summary='发送群合并转发',
    category='扩展',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='messages', type='message', required=False, desc='', default=None, role=''),
        QQParam(name='message', type='message', required=False, desc='', default=None, role=''),
        QQParam(name='source', type='string', required=False, desc='', default=None, role=''),
        QQParam(name='summary', type='string', required=False, desc='', default=None, role=''),
        QQParam(name='prompt', type='string', required=False, desc='', default=None, role=''),
        QQParam(name='news', type='raw', required=False, desc='', default=None, role=''),
    ),
    returns='{ message_id, res_id, forward_id }',
))
_register(QQAction(
    name='send_like',
    summary='点赞',
    category='扩展',
    read_only=False,
    risk='social',
    params=(
        QQParam(name='user_id', type='uint', required=True, desc='', default=None, role='user_id'),
        QQParam(name='times', type='int', required=False, desc='', default=1, role=''),
    ),
))
_register(QQAction(
    name='send_packet',
    summary='发送原始 SSO 包（cmd + hex data）',
    category='扩展',
    read_only=False,
    risk='credential',
    params=(
        QQParam(name='cmd', type='string', required=True, desc='', default=None, role=''),
        QQParam(name='data', type='string', required=False, desc='', default='', role=''),
        QQParam(name='rsp', type='bool', required=False, desc='', default=True, role=''),
    ),
    aliases=('.send_packet',),
))
_register(QQAction(
    name='send_poke',
    summary='拍一拍（群聊/私聊自动路由）',
    category='扩展',
    read_only=False,
    risk='social',
    params=(
        QQParam(name='user_id', type='uint', required=True, desc='', default=None, role='user_id'),
        QQParam(name='group_id', type='uint', required=False, desc='', default=None, role='group_id'),
    ),
))
_register(QQAction(
    name='send_private_forward_msg',
    summary='发送私聊合并转发',
    category='扩展',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='user_id', type='uint', required=True, desc='', default=None, role='user_id'),
        QQParam(name='messages', type='message', required=False, desc='', default=None, role=''),
        QQParam(name='message', type='message', required=False, desc='', default=None, role=''),
        QQParam(name='source', type='string', required=False, desc='', default=None, role=''),
        QQParam(name='summary', type='string', required=False, desc='', default=None, role=''),
        QQParam(name='prompt', type='string', required=False, desc='', default=None, role=''),
        QQParam(name='news', type='raw', required=False, desc='', default=None, role=''),
    ),
    returns='{ message_id, res_id, forward_id }',
))
_register(QQAction(
    name='send_tuwen_ark',
    summary='发送图文 Ark 卡片（私聊/群聊）',
    category='扩展',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='user_id', type='uint', required=False, desc='', default=None, role='user_id'),
        QQParam(name='group_id', type='uint', required=False, desc='', default=None, role='group_id'),
        QQParam(name='title', type='string', required=True, desc='', default=None, role=''),
        QQParam(name='desc', type='string', required=True, desc='', default=None, role=''),
        QQParam(name='summary', type='string', required=False, desc='', default='[分享]', role=''),
        QQParam(name='preview_url', type='string', required=False, desc='', default='https://tangram-1251316161.file.myqcloud.com/files/20210721/e50a8e37e08f29bf1ffc7466e1950690.png', role=''),
        QQParam(name='jump_url', type='string', required=True, desc='', default=None, role=''),
    ),
    returns='null',
))
_register(QQAction(
    name='set_diy_online_status',
    summary='设置自定义在线状态',
    category='扩展',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='face_id', type='int', required=True, desc='', default=None, role='face_id'),
        QQParam(name='face_type', type='int', required=False, desc='', default=1, role=''),
        QQParam(name='wording', type='string', required=False, desc='', default='', role=''),
    ),
))
_register(QQAction(
    name='set_doubt_friends_add_request',
    summary='处理可疑好友申请',
    category='扩展',
    read_only=False,
    risk='social',
    params=(
        QQParam(name='flag', type='string', required=True, desc='', default=None, role=''),
        QQParam(name='approve', type='bool', required=False, desc='', default=True, role=''),
    ),
))
_register(QQAction(
    name='set_essence_msg',
    summary='设置精华消息',
    category='扩展',
    read_only=False,
    risk='social',
    params=(
        QQParam(name='message_id', type='messageId', required=True, desc='', default=None, role='message_id'),
    ),
))
_register(QQAction(
    name='set_friend_remark',
    summary='设置好友备注',
    category='扩展',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='user_id', type='uint', required=True, desc='', default=None, role='user_id'),
        QQParam(name='remark', type='string', required=True, desc='', default=None, role=''),
    ),
))
_register(QQAction(
    name='set_friends_category',
    summary='移动好友到指定分组',
    category='扩展',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='uin', type='uint', required=True, desc='要移动的好友 QQ 号', default=None, role='user_id'),
        QQParam(name='categoryId', type='int', required=False, desc='目标分组 ID', default=None, role=''),
        QQParam(name='categoryName', type='string', required=False, desc='目标分组名称（必须唯一且完全匹配）', default=None, role=''),
    ),
    returns='成功时返回空数据。',
    invariants=('exactly one of: categoryId | categoryName',),
))
_register(QQAction(
    name='set_group_reaction',
    summary='群聊表情回应',
    category='扩展',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='group_id', type='uint', required=False, desc='', default=None, role='group_id'),
        QQParam(name='message_id', type='messageId', required=True, desc='', default=None, role='message_id'),
        QQParam(name='code', type='string', required=True, desc='', default=None, role=''),
        QQParam(name='is_set', type='bool', required=False, desc='', default=True, role=''),
    ),
))
_register(QQAction(
    name='set_group_remark',
    summary='设置群备注',
    category='扩展',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='', default=None, role='group_id'),
        QQParam(name='remark', type='string', required=True, desc='', default=None, role=''),
    ),
))
_register(QQAction(
    name='set_group_robot_add_option',
    summary='设置群机器人加群选项',
    category='扩展',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='robot_member_switch', type='int', required=False, desc='', default=None, role=''),
        QQParam(name='robot_member_examine', type='int', required=False, desc='', default=None, role=''),
    ),
))
_register(QQAction(
    name='set_group_sign',
    summary='群签到',
    category='扩展',
    read_only=False,
    risk='social',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
    ),
    aliases=('send_group_sign',),
))
_register(QQAction(
    name='set_group_todo',
    summary='设置群待办',
    category='扩展',
    read_only=False,
    risk='social',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='message_id', type='messageId', required=True, desc='', default=None, role='message_id'),
    ),
))
_register(QQAction(
    name='set_input_status',
    summary='设置输入状态',
    category='扩展',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='user_id', type='uint', required=True, desc='', default=None, role='user_id'),
        QQParam(name='event_type', type='int', required=False, desc='', default=0, role=''),
    ),
))
_register(QQAction(
    name='set_msg_emoji_like',
    summary='设置消息表情回应',
    category='扩展',
    read_only=False,
    risk='social',
    params=(
        QQParam(name='message_id', type='messageId', required=True, desc='', default=None, role='message_id'),
        QQParam(name='emoji_id', type='string', required=True, desc='', default=None, role=''),
        QQParam(name='set', type='bool', required=False, desc='', default=True, role=''),
    ),
))
_register(QQAction(
    name='set_online_status',
    summary='设置在线状态',
    category='扩展',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='status', type='int', required=True, desc='', default=None, role=''),
        QQParam(name='ext_status', type='int', required=False, desc='', default=0, role=''),
        QQParam(name='battery_status', type='int', required=False, desc='', default=100, role=''),
    ),
))
_register(QQAction(
    name='set_qq_avatar',
    summary='设置 QQ 头像',
    category='扩展',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='file', type='string', required=True, desc='', default=None, role='image'),
    ),
))
_register(QQAction(
    name='set_qq_profile',
    summary='设置 QQ 资料',
    category='扩展',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='nickname', type='string', required=False, desc='', default=None, role=''),
        QQParam(name='personal_note', type='string', required=False, desc='', default=None, role=''),
        QQParam(name='sex', type='int', required=False, desc='0 未知，1 男，2 女', default=None, role=''),
    ),
))
_register(QQAction(
    name='set_restart',
    summary='重启（不支持）',
    category='扩展',
    read_only=False,
    risk='send',
))
_register(QQAction(
    name='set_self_longnick',
    summary='设置个性签名（longNick/long_nick，严格 string）',
    category='扩展',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='longNick', type='string', required=False, desc='', default=None, role=''),
        QQParam(name='long_nick', type='string', required=False, desc='', default=None, role=''),
    ),
))
_register(QQAction(
    name='share_group_ex',
    summary='分享群 Ark 卡片',
    category='扩展',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='', default=None, role='group_id'),
    ),
    returns='服务端生成的群推荐 Ark 卡片 JSON 字符串。',
))
_register(QQAction(
    name='share_peer',
    summary='分享用户/群 Ark 卡片',
    category='扩展',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='user_id', type='uint', required=False, desc='', default=None, role='user_id'),
        QQParam(name='group_id', type='uint', required=False, desc='', default=None, role='group_id'),
        QQParam(name='phone_number', type='string', required=False, desc='', default='', role=''),
    ),
    returns='{ arkMsg }：服务端生成的推荐联系人 Ark 卡片 JSON 字符串。',
))
_register(QQAction(
    name='trans_group_file',
    summary='转存群文件',
    category='扩展',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='', default=None, role='group_id'),
        QQParam(name='file_id', type='string', required=True, desc='', default=None, role='file_id'),
    ),
    returns='{ ok: true }',
))
_register(QQAction(
    name='translate_en2zh',
    summary='英译中',
    category='扩展',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='words', type='string[]', required=True, desc='', default=None, role=''),
    ),
    returns='{ words }：与输入等长的中文译文字符串数组。',
))
_register(QQAction(
    name='upload_forward_msg',
    summary='上传转发消息',
    category='扩展',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='messages', type='message', required=False, desc='', default=None, role=''),
        QQParam(name='message', type='message', required=False, desc='', default=None, role=''),
        QQParam(name='group_id', type='uint', required=False, desc='', default=None, role='group_id'),
    ),
))
_register(QQAction(
    name='upload_foward_msg',
    summary='上传转发消息（别名拼写）',
    category='扩展',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='messages', type='message', required=False, desc='', default=None, role=''),
        QQParam(name='message', type='message', required=False, desc='', default=None, role=''),
        QQParam(name='group_id', type='uint', required=False, desc='', default=None, role='group_id'),
    ),
))
_register(QQAction(
    name='clean_stream_temp_file',
    summary='清理流式传输临时文件(仅清理 stream 上传/下载目录)',
    category='流式接口',
    read_only=False,
    risk='destructive',
    returns='{ message, removed }',
))
_register(QQAction(
    name='download_file_image_stream',
    summary='以流式方式下载图片(缓存图片 id / URL / stream 目录本地文件)',
    category='流式接口',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='file', type='string', required=False, desc='文件路径(限 stream 临时目录)/ http(s) URL', default=None, role='file'),
        QQParam(name='file_id', type='string', required=False, desc='文件 ID(缓存的图片/语音 id)', default=None, role='file_id'),
        QQParam(name='chunk_size', type='int', required=False, desc='分块大小(字节,默认 64KB)', default=None, role=''),
    ),
    returns='流式帧:file_info → file_chunk* → file_complete',
))
_register(QQAction(
    name='download_file_record_stream',
    summary='以流式方式下载语音(缓存语音 id / URL / stream 目录本地文件)',
    category='流式接口',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='file', type='string', required=False, desc='文件路径(限 stream 临时目录)/ http(s) URL', default=None, role='file'),
        QQParam(name='file_id', type='string', required=False, desc='文件 ID(缓存的图片/语音 id)', default=None, role='file_id'),
        QQParam(name='chunk_size', type='int', required=False, desc='分块大小(字节,默认 64KB)', default=None, role=''),
    ),
    returns='流式帧:file_info → file_chunk* → file_complete',
))
_register(QQAction(
    name='download_file_stream',
    summary='以流式方式下载文件(stream 目录本地文件 / URL / 缓存媒体)',
    category='流式接口',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='file', type='string', required=False, desc='文件路径(限 stream 临时目录)/ http(s) URL', default=None, role='file'),
        QQParam(name='file_id', type='string', required=False, desc='文件 ID(缓存的图片/语音 id)', default=None, role='file_id'),
        QQParam(name='chunk_size', type='int', required=False, desc='分块大小(字节,默认 64KB)', default=None, role=''),
    ),
    returns='流式帧:file_info → file_chunk* → file_complete',
))
_register(QQAction(
    name='test_download_stream',
    summary='测试下载流(推送 10 个数据帧,验证流式传输,不触达 QQ)',
    category='流式接口',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='error', type='bool', required=False, desc='是否触发测试错误', default=False, role=''),
    ),
    returns='流式帧:data_chunk*10 → data_complete(error=true 时以 error 帧结束)',
))
_register(QQAction(
    name='upload_file_stream',
    summary='以流式分块方式上传文件到机器人本地(返回可用于发送的本地路径)',
    category='流式接口',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='stream_id', type='string', required=True, desc='流 ID(客户端生成的 UUID,限 [A-Za-z0-9_-])', default=None, role=''),
        QQParam(name='chunk_data', type='string', required=False, desc='分块数据(Base64)', default=None, role=''),
        QQParam(name='chunk_index', type='int', required=False, desc='分块索引(从 0 开始)', default=None, role=''),
        QQParam(name='total_chunks', type='int', required=False, desc='总分块数(新流必填)', default=None, role=''),
        QQParam(name='file_size', type='int', required=False, desc='文件总大小(字节)', default=None, role=''),
        QQParam(name='expected_sha256', type='string', required=False, desc='期望的整文件 SHA256(校验)', default=None, role=''),
        QQParam(name='is_complete', type='bool', required=False, desc='是否为最后一个分块/触发合并', default=None, role=''),
        QQParam(name='filename', type='string', required=False, desc='文件名', default=None, role=''),
        QQParam(name='reset', type='bool', required=False, desc='重置并丢弃该流', default=None, role=''),
        QQParam(name='verify_only', type='bool', required=False, desc='仅查询当前流状态', default=None, role=''),
        QQParam(name='file_retention', type='int', required=False, desc='合并文件保留毫秒(0=不回收)', default=300000, role=''),
    ),
    returns='流式帧:分块确认 type=stream、完成 type=response(含 file_path/file_name/file_size/sha256)',
))
_register(QQAction(
    name='delete_msg',
    summary='撤回消息',
    category='消息',
    read_only=False,
    risk='destructive',
    params=(
        QQParam(name='message_id', type='messageId', required=True, desc='', default=None, role='message_id'),
    ),
))
_register(QQAction(
    name='get_msg',
    summary='获取消息',
    category='消息',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='message_id', type='messageId', required=True, desc='', default=None, role='message_id'),
    ),
    returns='消息事件对象（首次收到时存储的副本，已去除 post_type/self_id、附带 real_id 字段并刷新图片 URL）。',
))
_register(QQAction(
    name='send_group_msg',
    summary='发送群消息',
    category='消息',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='message', type='message', required=True, desc='', default=None, role=''),
        QQParam(name='auto_escape', type='bool', required=False, desc='', default=False, role=''),
    ),
    returns='{ message_id: number }',
))
_register(QQAction(
    name='send_msg',
    summary='发送消息（按 message_type/群号 自动路由群聊或私聊）',
    category='消息',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='message', type='message', required=True, desc='', default=None, role=''),
        QQParam(name='message_type', type='string', required=False, desc='', default=None, role=''),
        QQParam(name='group_id', type='uint', required=False, desc='', default=None, role='group_id'),
        QQParam(name='user_id', type='uint', required=False, desc='', default=None, role='user_id'),
        QQParam(name='auto_escape', type='bool', required=False, desc='', default=False, role=''),
    ),
    returns='{ message_id: number }',
))
_register(QQAction(
    name='send_private_msg',
    summary='发送私聊消息',
    category='消息',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='user_id', type='uint', required=True, desc='', default=None, role='user_id'),
        QQParam(name='message', type='message', required=True, desc='', default=None, role=''),
        QQParam(name='group_id', type='int', required=False, desc='', default=None, role=''),
        QQParam(name='auto_escape', type='bool', required=False, desc='', default=False, role=''),
    ),
    returns='{ message_id: number }',
))
_register(QQAction(
    name='comment_qzone',
    summary='评论一条说说（QQ 空间，支持纯文字或带图；传 images 自动上传）',
    category='空间',
    read_only=False,
    risk='social',
    params=(
        QQParam(name='tid', type='string', required=True, desc='说说 tid', default=None, role=''),
        QQParam(name='content', type='string', required=True, desc='评论内容', default=None, role=''),
        QQParam(name='target_uin', type='uint', required=False, desc='说说所属 QQ 号，省略则为机器人自己', default=None, role='user_id'),
        QQParam(name='images', type='string[]', required=False, desc='图片数组（可选），支持 file:// http:// base64://；自动上传', default=None, role=''),
    ),
))
_register(QQAction(
    name='delete_qzone_msg',
    summary='删除一条说说（QQ 空间，按 tid）',
    category='空间',
    read_only=False,
    risk='destructive',
    params=(
        QQParam(name='tid', type='string', required=True, desc='说说 tid（来自 get_qzone_msg_list / send_qzone_msg）', default=None, role=''),
    ),
))
_register(QQAction(
    name='get_qzone_feeds',
    summary='获取 QQ 空间好友动态（feed）；page_num 仅首页可靠，深翻页需时间游标（暂未实现）',
    category='空间',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='page_num', type='int', required=False, desc='页码（1 起；仅首页可靠）', default=1, role=''),
        QQParam(name='count', type='int', required=False, desc='本页数量', default=10, role=''),
    ),
    returns='好友动态对象，含本页 feed 数组与是否有更多页。',
))
_register(QQAction(
    name='get_qzone_msg_list',
    summary='获取 QQ 空间说说列表（默认机器人自己的空间）',
    category='空间',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='target_uin', type='uint', required=False, desc='目标 QQ 号，省略则取机器人自己', default=None, role='user_id'),
        QQParam(name='pos', type='int', required=False, desc='起始偏移', default=0, role=''),
        QQParam(name='num', type='int', required=False, desc='本页数量', default=20, role=''),
    ),
    returns='说说列表对象，含说说总数与本页说说数组。',
))
_register(QQAction(
    name='like_qzone',
    summary='给一条说说点赞（QQ 空间）',
    category='空间',
    read_only=False,
    risk='social',
    params=(
        QQParam(name='tid', type='string', required=True, desc='说说 tid', default=None, role=''),
        QQParam(name='target_uin', type='uint', required=False, desc='说说所属 QQ 号，省略则为机器人自己', default=None, role='user_id'),
        QQParam(name='abstime', type='int', required=False, desc='说说发表时间（unix 秒），传真实值更可靠', default=0, role='timestamp'),
    ),
))
_register(QQAction(
    name='send_qzone_msg',
    summary='发表说说（QQ 空间，支持纯文字或带图；传 images 自动上传；可设置查看权限）',
    category='空间',
    read_only=False,
    risk='social',
    params=(
        QQParam(name='content', type='string', required=True, desc='说说正文', default=None, role=''),
        QQParam(name='images', type='string[]', required=False, desc='图片数组（可选），支持 file:// http:// base64://；自动上传', default=None, role=''),
        QQParam(name='ugc_right', type='int', required=False, desc='查看权限：1=所有人可见，4=好友可见，16=部分好友可见，64=仅自己可见，128=部分好友不可见', default=1, role=''),
        QQParam(name='target_uins', type='uint[]', required=False, desc='权限作用 QQ 号数组；ugc_right=16 时表示可见名单，128 时表示不可见名单', default=None, role=''),
    ),
    invariants=('ugc_right must be one of 1, 4, 16, 64, 128', 'target_uins is required when ugc_right is 16 or 128',),
))
_register(QQAction(
    name='set_qzone_ban',
    summary='拉黑或解除拉黑某人（修改机器人自身 QQ 空间黑名单；enable=true 拉黑，false 解除）',
    category='空间',
    read_only=False,
    risk='destructive',
    params=(
        QQParam(name='user_id', type='uint', required=True, desc='目标 QQ 号', default=None, role='user_id'),
        QQParam(name='enable', type='bool', required=False, desc='true 拉黑，false 解除拉黑', default=True, role=''),
    ),
))
_register(QQAction(
    name='set_qzone_msg_right',
    summary='修改一条已发说说的查看权限（QQ 空间，按 tid）',
    category='空间',
    read_only=False,
    risk='social',
    params=(
        QQParam(name='tid', type='string', required=True, desc='说说 tid（来自 get_qzone_msg_list / send_qzone_msg）', default=None, role=''),
        QQParam(name='ugc_right', type='int', required=True, desc='查看权限：1=所有人可见，4=好友可见，16=部分好友可见，64=仅自己可见，128=部分好友不可见', default=None, role=''),
        QQParam(name='target_uins', type='uint[]', required=False, desc='权限作用 QQ 号数组；ugc_right=16 时表示可见名单，128 时表示不可见名单', default=None, role=''),
    ),
    returns='更新后的权限对象。',
    invariants=('ugc_right must be one of 1, 4, 16, 64, 128', 'target_uins is required when ugc_right is 16 or 128',),
))
_register(QQAction(
    name='unlike_qzone',
    summary='取消对一条说说的点赞（QQ 空间；取消赞端点待真机核实）',
    category='空间',
    read_only=False,
    risk='social',
    params=(
        QQParam(name='tid', type='string', required=True, desc='说说 tid', default=None, role=''),
        QQParam(name='target_uin', type='uint', required=False, desc='说说所属 QQ 号，省略则为机器人自己', default=None, role='user_id'),
        QQParam(name='abstime', type='int', required=False, desc='说说发表时间（unix 秒），传真实值更可靠', default=0, role='timestamp'),
    ),
))
_register(QQAction(
    name='fetch_face_entity',
    summary='按编号查询 QQ 系统表情',
    category='系统表情',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='face_id', type='int', required=True, desc='QQ 系统表情编号', default=None, role='face_id'),
        QQParam(name='refresh', type='bool', required=False, desc='是否强制从 QQ 刷新目录', default=False, role=''),
    ),
    returns='表情详情；编号不存在时返回 null。',
))
_register(QQAction(
    name='fetch_super_face_id',
    summary='判断 QQ 系统表情是否使用超级表情格式',
    category='系统表情',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='face_id', type='int', required=True, desc='QQ 系统表情编号', default=None, role='face_id'),
        QQParam(name='refresh', type='bool', required=False, desc='是否强制从 QQ 刷新目录', default=False, role=''),
    ),
    returns='是否使用超级表情格式。',
))
_register(QQAction(
    name='fetch_sys_faces',
    summary='获取 QQ 系统表情目录',
    category='系统表情',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='refresh', type='bool', required=False, desc='是否强制从 QQ 刷新目录', default=False, role=''),
    ),
    returns='按分组返回完整的 QQ 系统表情映射。',
))
_register(QQAction(
    name='search_sys_faces',
    summary='搜索 QQ 系统表情',
    category='系统表情',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='query', type='string', required=True, desc='编号、名称、别名或分组名', default=None, role=''),
    ),
    returns='匹配编号、名称、别名或分组名的表情列表。',
))
_register(QQAction(
    name='get_group_honor_info',
    summary='获取群荣誉信息',
    category='群信息',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='type', type='string', required=False, desc='', default=None, role=''),
    ),
))
_register(QQAction(
    name='get_group_info',
    summary='获取群信息',
    category='群信息',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='no_cache', type='bool', required=False, desc='', default=False, role=''),
    ),
    returns='群信息对象。',
))
_register(QQAction(
    name='get_group_list',
    summary='获取群列表',
    category='群信息',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='no_cache', type='bool', required=False, desc='', default=False, role=''),
    ),
    returns='群信息对象数组。',
))
_register(QQAction(
    name='get_group_member_info',
    summary='获取群成员信息',
    category='群信息',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='user_id', type='uint', required=True, desc='QQ 号', default=None, role='member_id'),
        QQParam(name='no_cache', type='bool', required=False, desc='', default=False, role=''),
    ),
    returns='群成员信息对象。',
))
_register(QQAction(
    name='get_group_member_list',
    summary='获取群成员列表',
    category='群信息',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='no_cache', type='bool', required=False, desc='', default=False, role=''),
    ),
    returns='群成员信息对象数组。',
))
_register(QQAction(
    name='get_group_system_msg',
    summary='获取群系统消息',
    category='群信息',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='group_id', type='uint', required=False, desc='', default=None, role='group_id'),
        QQParam(name='only_pending', type='bool', required=False, desc='', default=False, role=''),
        QQParam(name='count', type='int', required=False, desc='每个收件箱最多读取的记录数', default=50, role=''),
    ),
    returns='群系统消息数组，可按群号或未处理状态过滤。',
))
_register(QQAction(
    name='create_group_file_folder',
    summary='创建群文件夹',
    category='群文件',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='name', type='string', required=True, desc='', default=None, role=''),
        QQParam(name='parent_id', type='string', required=False, desc='', default='/', role=''),
    ),
    returns='{ result: { retCode, retMsg }, groupItem: { folderInfo: { folderId, folderName, folderPath, createTime, modifyTime, createUin, modifyUin } } }',
))
_register(QQAction(
    name='delete_group_file',
    summary='删除群文件',
    category='群文件',
    read_only=False,
    risk='destructive',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='file_id', type='string', required=True, desc='', default=None, role='file_id'),
    ),
))
_register(QQAction(
    name='delete_group_file_folder',
    summary='删除群文件夹',
    category='群文件',
    read_only=False,
    risk='destructive',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='folder_id', type='string', required=True, desc='', default=None, role=''),
    ),
))
_register(QQAction(
    name='get_group_file_url',
    summary='获取群文件下载链接',
    category='群文件',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='file_id', type='string', required=True, desc='', default=None, role='file_id'),
        QQParam(name='busid', type='raw', required=False, desc='', default=None, role=''),
    ),
    returns='群文件下载链接。',
))
_register(QQAction(
    name='get_group_files_by_folder',
    summary='获取群子目录文件列表',
    category='群文件',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='folder_id', type='string', required=False, desc='', default='', role=''),
        QQParam(name='folder', type='string', required=False, desc='', default='', role=''),
    ),
    returns='群文件系统信息（文件与文件夹列表）。',
))
_register(QQAction(
    name='get_group_root_files',
    summary='获取群根目录文件列表',
    category='群文件',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
    ),
    returns='群文件系统信息（文件与文件夹列表）。',
))
_register(QQAction(
    name='get_private_file_url',
    summary='获取私聊文件下载链接',
    category='群文件',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='user_id', type='uint', required=False, desc='', default=None, role='user_id'),
        QQParam(name='file_id', type='string', required=True, desc='', default=None, role='file_id'),
        QQParam(name='file_hash', type='string', required=False, desc='', default='', role=''),
    ),
    returns='私聊文件下载链接。',
))
_register(QQAction(
    name='move_group_file',
    summary='移动群文件',
    category='群文件',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='file_id', type='string', required=True, desc='', default=None, role='file_id'),
        QQParam(name='parent_directory', type='string', required=True, desc='', default=None, role=''),
        QQParam(name='target_directory', type='string', required=True, desc='', default=None, role=''),
    ),
))
_register(QQAction(
    name='rename_group_file',
    summary='重命名群文件',
    category='群文件',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='file_id', type='string', required=True, desc='', default=None, role='file_id'),
        QQParam(name='current_parent_directory', type='string', required=False, desc='', default='/', role=''),
        QQParam(name='new_name', type='string', required=True, desc='', default=None, role=''),
    ),
))
_register(QQAction(
    name='rename_group_file_folder',
    summary='重命名群文件夹',
    category='群文件',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='folder_id', type='string', required=True, desc='', default=None, role=''),
        QQParam(name='new_folder_name', type='string', required=False, desc='', default='', role=''),
        QQParam(name='name', type='string', required=False, desc='', default='', role=''),
    ),
))
_register(QQAction(
    name='upload_group_file',
    summary='上传群文件',
    category='群文件',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='file', type='string', required=True, desc='', default=None, role='file'),
        QQParam(name='name', type='string', required=False, desc='', default='', role=''),
        QQParam(name='folder', type='string', required=False, desc='', default='', role=''),
        QQParam(name='folder_id', type='string', required=False, desc='', default='', role=''),
        QQParam(name='upload_file', type='bool', required=False, desc='', default=True, role=''),
    ),
    returns='{ file_id: string }',
))
_register(QQAction(
    name='upload_private_file',
    summary='上传私聊文件',
    category='群文件',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='user_id', type='uint', required=True, desc='', default=None, role='user_id'),
        QQParam(name='file', type='string', required=True, desc='', default=None, role='file'),
        QQParam(name='name', type='string', required=False, desc='', default='', role=''),
        QQParam(name='upload_file', type='bool', required=False, desc='', default=True, role=''),
    ),
    returns='{ file_id: string }',
))
_register(QQAction(
    name='cancel_group_album_media_like',
    summary='',
    category='群相册',
    read_only=False,
    risk='social',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='album_id', type='string', required=True, desc='', default=None, role=''),
        QQParam(name='batch_id', type='string', required=True, desc='', default=None, role=''),
        QQParam(name='lloc', type='string', required=False, desc='', default=None, role=''),
    ),
))
_register(QQAction(
    name='del_group_album_media',
    summary='删除群相册图片或视频',
    category='群相册',
    read_only=False,
    risk='destructive',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='album_id', type='string', required=True, desc='', default=None, role=''),
        QQParam(name='lloc', type='string', required=True, desc='图片长定位标识，或视频 id', default=None, role=''),
    ),
))
_register(QQAction(
    name='do_group_album_comment',
    summary='',
    category='群相册',
    read_only=False,
    risk='social',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='album_id', type='string', required=True, desc='', default=None, role=''),
        QQParam(name='lloc', type='string', required=True, desc='', default=None, role=''),
        QQParam(name='content', type='string', required=True, desc='', default=None, role=''),
    ),
))
_register(QQAction(
    name='get_group_album_list',
    summary='',
    category='群相册',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
    ),
    returns='群相册列表数组，每项为一个相册的基本信息。',
))
_register(QQAction(
    name='get_group_album_media_list',
    summary='',
    category='群相册',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='album_id', type='string', required=True, desc='', default=None, role=''),
        QQParam(name='attach_info', type='string', required=False, desc='', default='', role=''),
    ),
    returns='相册图片/视频列表及下一页分页游标；视频项包含 id、url、cover、尺寸、时长和多规格地址。',
))
_register(QQAction(
    name='get_qun_album_list',
    summary='',
    category='群相册',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='attach_info', type='string', required=False, desc='', default='', role=''),
    ),
    returns='NapCat 风格的相册列表封套：{album_list, attach_info, has_more}。',
))
_register(QQAction(
    name='set_group_album_media_like',
    summary='',
    category='群相册',
    read_only=False,
    risk='social',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='album_id', type='string', required=True, desc='', default=None, role=''),
        QQParam(name='batch_id', type='string', required=True, desc='', default=None, role=''),
        QQParam(name='lloc', type='string', required=False, desc='', default=None, role=''),
    ),
))
_register(QQAction(
    name='upload_image_to_qun_album',
    summary='',
    category='群相册',
    read_only=False,
    risk='social',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='album_id', type='string', required=True, desc='', default=None, role=''),
        QQParam(name='album_name', type='string', required=True, desc='', default=None, role=''),
        QQParam(name='file', type='string', required=True, desc='', default=None, role='image'),
    ),
))
_register(QQAction(
    name='get_group_admin_settings',
    summary='获取群管理设置的当前值',
    category='群管理',
    read_only=True,
    risk='read',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
    ),
    returns='与对应设置接口字段对齐的当前群管理设置。',
))
_register(QQAction(
    name='set_group_add_option',
    summary='设置加群选项',
    category='群管理',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='add_type', type='int', required=False, desc='', default=0, role=''),
        QQParam(name='group_question', type='string', required=False, desc='', default=None, role=''),
        QQParam(name='group_answer', type='string', required=False, desc='', default=None, role=''),
    ),
))
_register(QQAction(
    name='set_group_admin',
    summary='设置/取消管理员',
    category='群管理',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='user_id', type='uint', required=True, desc='QQ 号', default=None, role='member_id'),
        QQParam(name='enable', type='bool', required=False, desc='', default=True, role=''),
    ),
))
_register(QQAction(
    name='set_group_anonymous',
    summary='匿名开关（未实现，返回 ok）',
    category='群管理',
    read_only=False,
    risk='send',
))
_register(QQAction(
    name='set_group_anonymous_ban',
    summary='匿名禁言（未实现，返回 ok）',
    category='群管理',
    read_only=False,
    risk='destructive',
))
_register(QQAction(
    name='set_group_ban',
    summary='禁言群成员（duration=0 解除）',
    category='群管理',
    read_only=False,
    risk='destructive',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='user_id', type='uint', required=True, desc='QQ 号', default=None, role='member_id'),
        QQParam(name='duration', type='int', required=False, desc='', default=1800, role='duration'),
    ),
))
_register(QQAction(
    name='set_group_card',
    summary='设置群名片（空字符串清除）',
    category='群管理',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='user_id', type='uint', required=True, desc='QQ 号', default=None, role='member_id'),
        QQParam(name='card', type='string', required=False, desc='', default='', role=''),
    ),
))
_register(QQAction(
    name='set_group_kick',
    summary='踢出群成员',
    category='群管理',
    read_only=False,
    risk='destructive',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='user_id', type='uint', required=True, desc='QQ 号', default=None, role='member_id'),
        QQParam(name='reject_add_request', type='bool', required=False, desc='', default=False, role=''),
    ),
))
_register(QQAction(
    name='set_group_kick_members',
    summary='批量踢出群成员',
    category='群管理',
    read_only=False,
    risk='destructive',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='user_id', type='uint[]', required=True, desc='', default=None, role=''),
        QQParam(name='reject_add_request', type='bool', required=False, desc='', default=False, role=''),
    ),
))
_register(QQAction(
    name='set_group_leave',
    summary='退群',
    category='群管理',
    read_only=False,
    risk='destructive',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
    ),
))
_register(QQAction(
    name='set_group_member_invite_policy',
    summary='设置群成员邀请策略',
    category='群管理',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='policy', type='enum', required=True, desc='', default=None, role=''),
    ),
))
_register(QQAction(
    name='set_group_member_permissions',
    summary='设置群成员权限（仅群主可改）',
    category='群管理',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='allow_member_upload_album', type='bool', required=False, desc='', default=None, role=''),
        QQParam(name='allow_member_temporary_session', type='bool', required=False, desc='', default=None, role=''),
        QQParam(name='allow_member_create_group', type='bool', required=False, desc='', default=None, role=''),
    ),
    invariants=('at least one of: allow_member_upload_album, allow_member_temporary_session, allow_member_create_group',),
))
_register(QQAction(
    name='set_group_name',
    summary='设置群名',
    category='群管理',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='group_name', type='string', required=False, desc='', default='', role=''),
    ),
))
_register(QQAction(
    name='set_group_new_member_history_visibility',
    summary='设置新成员是否可查看历史消息',
    category='群管理',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='visible', type='bool', required=True, desc='', default=None, role=''),
    ),
))
_register(QQAction(
    name='set_group_portrait',
    summary='设置群头像',
    category='群管理',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='file', type='string', required=True, desc='', default=None, role='image'),
    ),
))
_register(QQAction(
    name='set_group_search',
    summary='设置群被搜索方式（群指纹 / 群号搜索开关）',
    category='群管理',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='no_finger_open', type='int', required=False, desc='', default=None, role=''),
        QQParam(name='no_code_finger_open', type='int', required=False, desc='', default=None, role=''),
    ),
))
_register(QQAction(
    name='set_group_special_title',
    summary='设置群头衔',
    category='群管理',
    read_only=False,
    risk='send',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='user_id', type='uint', required=True, desc='QQ 号', default=None, role='member_id'),
        QQParam(name='special_title', type='string', required=False, desc='', default='', role=''),
    ),
))
_register(QQAction(
    name='set_group_whole_ban',
    summary='全员禁言开关',
    category='群管理',
    read_only=False,
    risk='destructive',
    params=(
        QQParam(name='group_id', type='uint', required=True, desc='群号', default=None, role='group_id'),
        QQParam(name='enable', type='bool', required=False, desc='', default=True, role=''),
    ),
))
_register(QQAction(
    name='set_friend_add_request',
    summary='处理好友添加请求',
    category='请求',
    read_only=False,
    risk='social',
    params=(
        QQParam(name='flag', type='string', required=True, desc='', default=None, role=''),
        QQParam(name='approve', type='bool', required=False, desc='', default=True, role=''),
    ),
))
_register(QQAction(
    name='set_group_add_request',
    summary='处理加群请求',
    category='请求',
    read_only=False,
    risk='social',
    params=(
        QQParam(name='flag', type='string', required=True, desc='', default=None, role=''),
        QQParam(name='sub_type', type='string', required=False, desc='', default=None, role=''),
        QQParam(name='type', type='string', required=False, desc='', default=None, role=''),
        QQParam(name='approve', type='bool', required=False, desc='', default=True, role=''),
        QQParam(name='reason', type='string', required=False, desc='', default='', role=''),
    ),
))

ACTION_ALIASES: dict[str, str] = {}
for _action in ACTIONS.values():
    for _alias in _action.aliases:
        ACTION_ALIASES.setdefault(_alias, _action.name)


def resolve(name: str) -> QQAction | None:
    """按动作名（含别名）取描述；未知动作返回 None。"""
    key = str(name or "").strip()
    if not key:
        return None
    if key in ACTIONS:
        return ACTIONS[key]
    canonical = ACTION_ALIASES.get(key)
    return ACTIONS.get(canonical) if canonical else None


# 类型转换表：把 LLM 送来的松散值转成动作期望的类型（尽力而为，转不了就原样）
_INT_TYPES = {"uint", "int", "messageId", "number"}
_BOOL_TYPES = {"bool"}


def coerce(value: Any, type_name: str) -> Any:
    """按动作参数类型做一次宽松转换——LLM 常把数字/布尔写成字符串。"""
    if not isinstance(value, str):
        if type_name in _INT_TYPES and isinstance(value, bool):
            return int(value)
        return value
    text = value.strip()
    if not text:
        return value
    if type_name in _INT_TYPES:
        try:
            return int(text)
        except ValueError:
            return value
    if type_name in _BOOL_TYPES:
        lowered = text.lower()
        if lowered in {"true", "1", "yes", "y", "on"}:
            return True
        if lowered in {"false", "0", "no", "n", "off"}:
            return False
    if type_name.endswith("[]") and "," in text:
        inner = type_name[:-2]
        return [coerce(part, inner) for part in text.split(",") if part.strip()]
    return value


def normalize_params(action: QQAction, params: Mapping[str, Any],
                     *, strip_unknown: bool = True) -> tuple[dict[str, Any], list[str]]:
    """校验并归一化入参：返回 (clean, missing)。

    - 已知参数：按声明类型做宽松转换；
    - 未声明参数：动作 `accepts_extra` 为真时原样保留（SnowLuma 允许透传），
      否则丢弃（strip_unknown=False 时保留，供内部调用方用）；
    - `missing`：必填缺失列表（调用方决定是报错还是提醒）。
    """
    clean: dict[str, Any] = {}
    missing: list[str] = []
    known = {p.name: p for p in action.params}
    for name, spec in known.items():
        if name in params and params[name] is not None:
            clean[name] = coerce(params[name], spec.type)
        elif spec.required:
            missing.append(name)
    for name, value in (params or {}).items():
        if name in known or value is None:
            continue
        if action.accepts_extra or not strip_unknown:
            clean[name] = value
    return clean, missing


def describe(action: QQAction, *, with_params: bool = True) -> str:
    """一行动作说明（catalog 展示用）。"""
    bits = [f"{action.name} [{action.category}]"]
    if action.read_only:
        bits.append("(只读)")
    if action.summary:
        bits.append(action.summary)
    if with_params and action.params:
        parts = []
        for p in action.params:
            mark = "必填" if p.required else "可选"
            extra = f" 默认{p.default}" if p.default is not None else ""
            desc = f" {p.desc}" if p.desc else ""
            parts.append(f"{p.name}({p.type},{mark}{extra}){desc}")
        bits.append("参数：" + "；".join(parts))
    if action.returns:
        bits.append("返回：" + action.returns)
    return " | ".join(bits)


def names_in(category: str) -> list[str]:
    return [a.name for a in ACTIONS.values() if a.category == category]


def stats() -> dict[str, int]:
    out: dict[str, int] = {}
    for action in ACTIONS.values():
        out[action.category] = out.get(action.category, 0) + 1
    return out

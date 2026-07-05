import asyncio
import json
import logging
import os
from functools import wraps
from pathlib import Path
from urllib.parse import quote

from telegram import (
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestChat,
    KeyboardButtonRequestUsers,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import KeyboardButtonStyle, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# --------------------------------------------
# ENVIRONMENT VARIABLES (no hardcoded defaults)
# --------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_IDS = {
    int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x
}
LOG_CHANNEL_ID = os.environ.get("LOG_CHANNEL_ID")   # None if not set

# request_id values used to distinguish which keyboard button was pressed
REQ_USER = 1
REQ_PREMIUM = 2
REQ_BOT = 3
REQ_GROUP = 4
REQ_CHANNEL = 5
REQ_FORUM = 6
REQ_MY_GROUP = 7
REQ_MY_CHANNEL = 8
REQ_MY_FORUM = 9

KIND_CODES = {
    "user": "u",
    "premium user": "pu",
    "bot": "b",
    "group": "g",
    "channel": "c",
    "forum": "f",
    "chat": "ch",
}

_SMALL_CAPS = {
    "a": "ᴀ", "b": "ʙ", "c": "ᴄ", "d": "ᴅ", "e": "ᴇ", "f": "ꜰ", "g": "ɢ",
    "h": "ʜ", "i": "ɪ", "j": "ᴊ", "k": "ᴋ", "l": "ʟ", "m": "ᴍ", "n": "ɴ",
    "o": "ᴏ", "p": "ᴘ", "q": "ǫ", "r": "ʀ", "t": "ᴛ", "u": "ᴜ", "v": "ᴠ",
    "w": "ᴡ", "y": "ʏ", "z": "ᴢ",
}


# --------------------------------------------------------------------------
# Storage (plain JSON file for now — swap this module out later for MongoDB
# without touching any handler code; every handler only calls the functions
# below, never touches STORE/the file directly).
# --------------------------------------------------------------------------
STORE_PATH = Path(__file__).parent / "store.json"


def _default_store() -> dict:
    return {"users": [], "groups": [], "channels": [], "force_channels": []}


def load_store() -> dict:
    if STORE_PATH.exists():
        try:
            with open(STORE_PATH, "r") as f:
                data = json.load(f)
            for key, default in _default_store().items():
                data.setdefault(key, default)
            return data
        except (json.JSONDecodeError, OSError):
            logger.warning("store.json unreadable/corrupted, starting fresh")
    return _default_store()


STORE = load_store()


def save_store() -> None:
    tmp_path = STORE_PATH.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        json.dump(STORE, f)
    tmp_path.replace(STORE_PATH)


def track_chat(kind: str, chat_id: int) -> bool:
    bucket = STORE.setdefault(kind, [])
    if chat_id not in bucket:
        bucket.append(chat_id)
        save_store()
        return True
    return False


def untrack_chat(chat_id: int) -> None:
    changed = False
    for kind in ("users", "groups", "channels"):
        bucket = STORE.get(kind, [])
        if chat_id in bucket:
            bucket.remove(chat_id)
            changed = True
    if changed:
        save_store()


def get_stats() -> dict:
    return {
        "users": len(STORE.get("users", [])),
        "groups": len(STORE.get("groups", [])),
        "channels": len(STORE.get("channels", [])),
        "force_channels": len(STORE.get("force_channels", [])),
    }


def small_caps(text: str) -> str:
    """Convert text to small-caps unicode style (s/x stay lowercase)."""
    return "".join(_SMALL_CAPS.get(ch.lower(), ch) for ch in text)


def html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def build_id_message(bot_username: str, kind: str, id_value: int, name: str | None = None) -> str:
    """Build the quoted, small-caps ID message used everywhere in the bot.

    Renders as two separate blockquotes:
        > 💎ɪᴅ ᴄʜᴇᴄᴋᴇʀ ʙᴏᴛ @bot_username
        > 🆔 ᴛʜᴀᴛ <kind> ɪᴅ ɪs - id
    plus the entity's name below, when known.
    """
    brand_line = f"💎{small_caps('id checker bot')} @{bot_username}"
    id_line = f"🆔 {small_caps(f'that {kind} id is')} - <code>{id_value}</code>"
    text = f"<blockquote>{html_escape(brand_line)}</blockquote>"
    text += f"\n<blockquote>{id_line}</blockquote>"
    if name:
        text += f"\n📛 {small_caps('name')} : {html_escape(name)}"
    return text


def build_share_text(bot_username: str, kind: str, id_value: int, name: str | None = None) -> str:
    """Plain-text version of the ID message for the "Share ID" deep link.

    t.me/share/url pre-fills a chat's message box with plain text (no HTML
    parsing), so we recreate the quoted look using literal "> " prefixes
    instead of <blockquote> tags.
    """
    brand_line = f"💎{small_caps('id checker bot')} @{bot_username}"
    id_line = f"🆔 {small_caps(f'that {kind} id is')} - {id_value}"
    text = f"> {brand_line}\n> {id_line}"
    if name:
        text += f"\n📛 {small_caps('name')} : {name}"
    return text


def id_reply_markup(
    bot_username: str, id_value: int, kind: str = "user", name: str | None = None
) -> InlineKeyboardMarkup:
    """Copy + Share ID buttons, same as the reference screenshots.

    Share ID uses a https://t.me/share/url deep link (not switch_inline_query)
    so tapping it opens Telegram's native "choose a chat" picker without
    requiring the bot's inline mode to be enabled via @BotFather. The picked
    chat receives the pre-filled text exactly as composed here, unaffected by
    bot restarts since nothing is cached — the id/name are baked into the link.
    """
    share_text = build_share_text(bot_username=bot_username, kind=kind, id_value=id_value, name=name)
    share_url = f"https://t.me/share/url?text={quote(share_text)}"
    keyboard = [
        [
            InlineKeyboardButton(
                "📋 Copy ID",
                copy_text=CopyTextButton(text=str(id_value)),
                style=KeyboardButtonStyle.PRIMARY,
            )
        ],
        [
            InlineKeyboardButton(
                "🚀 Share ID",
                url=share_url,
                style=KeyboardButtonStyle.DANGER,
            )
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def main_menu_keyboard(is_admin_user: bool = False) -> ReplyKeyboardMarkup:
    """Build the same 3x3 menu shown in the reference screenshots.

    When `is_admin_user` is True, an extra "🛠 Admin Panel" row is appended —
    this row is only ever sent to admins, so regular users never see it.
    """
    keyboard = [
        [
            KeyboardButton(
                "👤 User",
                request_users=KeyboardButtonRequestUsers(
                    request_id=REQ_USER,
                    user_is_bot=False,
                    request_name=True,
                    request_username=True,
                ),
                style=KeyboardButtonStyle.DANGER,
            ),
            KeyboardButton(
                "🌟 Premium",
                request_users=KeyboardButtonRequestUsers(
                    request_id=REQ_PREMIUM,
                    user_is_bot=False,
                    user_is_premium=True,
                    request_name=True,
                    request_username=True,
                ),
                style=KeyboardButtonStyle.DANGER,
            ),
            KeyboardButton(
                "🤖 Bot",
                request_users=KeyboardButtonRequestUsers(
                    request_id=REQ_BOT,
                    user_is_bot=True,
                    request_name=True,
                    request_username=True,
                ),
                style=KeyboardButtonStyle.DANGER,
            ),
        ],
        [
            KeyboardButton(
                "👥 Group",
                request_chat=KeyboardButtonRequestChat(
                    request_id=REQ_GROUP,
                    chat_is_channel=False,
                    request_title=True,
                    request_username=True,
                ),
                style=KeyboardButtonStyle.PRIMARY,
            ),
            KeyboardButton(
                "📢 Channel",
                request_chat=KeyboardButtonRequestChat(
                    request_id=REQ_CHANNEL,
                    chat_is_channel=True,
                    request_title=True,
                    request_username=True,
                ),
                style=KeyboardButtonStyle.PRIMARY,
            ),
            KeyboardButton(
                "💬 Forum",
                request_chat=KeyboardButtonRequestChat(
                    request_id=REQ_FORUM,
                    chat_is_channel=False,
                    chat_is_forum=True,
                    request_title=True,
                    request_username=True,
                ),
                style=KeyboardButtonStyle.PRIMARY,
            ),
        ],
        [
            KeyboardButton(
                "👥 My Group",
                request_chat=KeyboardButtonRequestChat(
                    request_id=REQ_MY_GROUP,
                    chat_is_channel=False,
                    chat_is_created=True,
                    request_title=True,
                    request_username=True,
                ),
                style=KeyboardButtonStyle.SUCCESS,
            ),
            KeyboardButton(
                "📢 My Channel",
                request_chat=KeyboardButtonRequestChat(
                    request_id=REQ_MY_CHANNEL,
                    chat_is_channel=True,
                    chat_is_created=True,
                    request_title=True,
                    request_username=True,
                ),
                style=KeyboardButtonStyle.SUCCESS,
            ),
            KeyboardButton(
                "💬 My Forum",
                request_chat=KeyboardButtonRequestChat(
                    request_id=REQ_MY_FORUM,
                    chat_is_channel=False,
                    chat_is_forum=True,
                    chat_is_created=True,
                    request_title=True,
                    request_username=True,
                ),
                style=KeyboardButtonStyle.SUCCESS,
            ),
        ],
    ]
    if is_admin_user:
        keyboard.append([KeyboardButton("🛠 Admin Panel")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


# --------------------------------------------------------------------------
# Force-join gate
# --------------------------------------------------------------------------
async def get_missing_force_channels(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> list[dict]:
    missing = []
    for ch in STORE.get("force_channels", []):
        try:
            member = await context.bot.get_chat_member(ch["chat_id"], user_id)
            if member.status in ("left", "kicked"):
                missing.append(ch)
        except TelegramError:
            missing.append(ch)
    return missing


def force_join_markup(missing: list[dict]) -> InlineKeyboardMarkup:
    buttons = []
    for ch in missing:
        username = ch.get("username")
        invite_link = ch.get("invite_link")
        label = f"📢 {ch.get('title') or username or ch['chat_id']}"
        link = f"https://t.me/{username}" if username else invite_link
        if link:
            buttons.append([InlineKeyboardButton(label, url=link)])
        else:
            buttons.append([InlineKeyboardButton(label, callback_data="noop")])
    buttons.append([InlineKeyboardButton("✅ I've Joined", callback_data="check_join")])
    return InlineKeyboardMarkup(buttons)


async def send_force_join_prompt(update: Update, missing: list[dict]) -> None:
    text = (
        "🚫 ᴘʟᴇᴀꜱᴇ ᴊᴏɪɴ ᴛʜᴇ ꜰᴏʟʟᴏᴡɪɴɢ ᴛᴏ ᴜꜱᴇ ᴛʜɪꜱ ʙᴏᴛ, ᴛʜᴇɴ ᴛᴀᴘ ✅ ɪ'ᴠᴇ ᴊᴏɪɴᴇᴅ:"
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=force_join_markup(missing))
    elif update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=force_join_markup(missing))


def require_force_join(handler):
    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        user = update.effective_user
        if chat is None or chat.type != "private" or user is None:
            return await handler(update, context)
        if user.id in ADMIN_IDS or not STORE.get("force_channels"):
            return await handler(update, context)
        missing = await get_missing_force_channels(context, user.id)
        if missing:
            await send_force_join_prompt(update, missing)
            return
        return await handler(update, context)

    return wrapper


async def check_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user
    missing = await get_missing_force_channels(context, user.id)
    if missing:
        await query.answer("❌ Aap ne abhi tak sab join nahi kiya hai.", show_alert=True)
        return
    await query.answer("✅ Shukriya! Ab bot use kar sakte hain.")
    await query.message.delete()
    text = build_id_message(context.bot.username, "user", user.id, name=user.full_name)
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=id_reply_markup(context.bot.username, user.id, "user", user.full_name),
    )
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="Choose an option below to get any User / Group / Channel ID 👇",
        reply_markup=main_menu_keyboard(is_admin(user.id)),
    )


async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()


# --------------------------------------------------------------------------
# Chat tracking (drives Stats + Broadcast — every chat the bot has ever
# talked to or been added to gets recorded here)
# --------------------------------------------------------------------------
async def send_log(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    if not LOG_CHANNEL_ID:
        return
    try:
        await context.bot.send_message(
            chat_id=LOG_CHANNEL_ID, text=text, parse_mode=ParseMode.HTML
        )
    except TelegramError as e:
        logger.warning("Couldn't send log to LOG_CHANNEL_ID: %s", e)


async def track_chat_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    if chat.type == "private":
        is_new = track_chat("users", chat.id)
        context.user_data["_is_new_user"] = is_new
    elif chat.type in ("group", "supergroup"):
        is_new = track_chat("groups", chat.id)
        if is_new:
            await send_log(
                context,
                "🆕 <b>New group added the bot</b>\n"
                f"📛 Title: {chat.title}\n"
                f"🆔 ID: <code>{chat.id}</code>",
            )
    elif chat.type == "channel":
        is_new = track_chat("channels", chat.id)
        if is_new:
            await send_log(
                context,
                "🆕 <b>New channel added the bot</b>\n"
                f"📛 Title: {chat.title}\n"
                f"🆔 ID: <code>{chat.id}</code>",
            )


async def my_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    result = update.my_chat_member
    if result is None:
        return
    chat = result.chat
    new_status = result.new_chat_member.status
    if new_status in ("left", "kicked"):
        untrack_chat(chat.id)
    elif chat.type == "channel":
        track_chat("channels", chat.id)
    elif chat.type in ("group", "supergroup"):
        track_chat("groups", chat.id)


# --------------------------------------------------------------------------
# Main bot handlers
# --------------------------------------------------------------------------
@require_force_join
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    text = build_id_message(context.bot.username, "user", user.id, name=user.full_name)
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=id_reply_markup(context.bot.username, user.id, "user", user.full_name),
    )
    await update.message.reply_text(
        "Choose an option below to get any User / Group / Channel ID 👇",
        reply_markup=main_menu_keyboard(is_admin(user.id)),
    )

    is_new = context.user_data.pop("_is_new_user", False)
    log_text = (
        f"{'🆕 <b>New user started the bot</b>' if is_new else '🔁 <b>Old user started the bot</b>'}\n"
        f"👤 Name: {user.full_name}\n"
        f"🆔 ID: <code>{user.id}</code>"
    )
    if user.username:
        log_text += f"\n🔗 Username: @{user.username}"
    await send_log(context, log_text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "ℹ️ *How To Use*\n\n"
        "👤 *User* — pick any user to get their ID\n"
        "🌟 *Premium* — pick a Telegram Premium user\n"
        "🤖 *Bot* — pick any bot to get its ID\n"
        "👥 *Group* — pick any group to get its ID\n"
        "📢 *Channel* — pick any channel to get its ID\n"
        "💬 *Forum* — pick any forum group to get its ID\n"
        "👥 *My Group* — pick a group you own\n"
        "📢 *My Channel* — pick a channel you own\n"
        "💬 *My Forum* — pick a forum you own\n\n"
        "You can also just forward me any message to get the sender's ID, "
        "or add me to a group/channel to get its ID."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


@require_force_join
async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    name = chat.title or chat.full_name
    text = build_id_message(context.bot.username, "chat", chat.id, name=name)
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=id_reply_markup(context.bot.username, chat.id, "chat", name),
    )
    requester = update.effective_user
    await send_log(
        context,
        "🔎 <b>User search</b> (/id)\n"
        f"👤 By: {requester.full_name} (<code>{requester.id}</code>)\n"
        f"🎯 Looked up: {name} — <code>{chat.id}</code>",
    )


@require_force_join
async def users_shared(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    shared = update.message.users_shared
    kinds = {
        REQ_USER: "user",
        REQ_PREMIUM: "premium user",
        REQ_BOT: "bot",
    }
    kind = kinds.get(shared.request_id, "user")
    requester = update.effective_user
    for u in shared.users:
        name = " ".join(filter(None, [u.first_name, u.last_name])) or None
        text = build_id_message(context.bot.username, kind, u.user_id, name=name)
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=id_reply_markup(context.bot.username, u.user_id, kind, name),
        )
        await send_log(
            context,
            f"🔎 <b>User search</b> ({kind})\n"
            f"👤 By: {requester.full_name} (<code>{requester.id}</code>)\n"
            f"🎯 Looked up: {name or 'Unknown'} — <code>{u.user_id}</code>",
        )


@require_force_join
async def chat_shared(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    shared = update.message.chat_shared
    kinds = {
        REQ_GROUP: "group",
        REQ_CHANNEL: "channel",
        REQ_FORUM: "forum",
        REQ_MY_GROUP: "group",
        REQ_MY_CHANNEL: "channel",
        REQ_MY_FORUM: "forum",
    }
    kind = kinds.get(shared.request_id, "chat")
    name = shared.title
    text = build_id_message(context.bot.username, kind, shared.chat_id, name=name)
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=id_reply_markup(context.bot.username, shared.chat_id, kind, name),
    )
    requester = update.effective_user
    await send_log(
        context,
        f"🔎 <b>User search</b> ({kind})\n"
        f"👤 By: {requester.full_name} (<code>{requester.id}</code>)\n"
        f"🎯 Looked up: {name or 'Unknown'} — <code>{shared.chat_id}</code>",
    )


@require_force_join
async def forwarded_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    origin = msg.forward_origin
    if origin is None:
        return

    requester = update.effective_user
    if hasattr(origin, "sender_user") and origin.sender_user is not None:
        uid = origin.sender_user.id
        name = origin.sender_user.full_name
        text = build_id_message(context.bot.username, "user", uid, name=name)
        await msg.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=id_reply_markup(context.bot.username, uid, "user", name),
        )
        await send_log(
            context,
            "🔎 <b>User search</b> (forwarded)\n"
            f"👤 By: {requester.full_name} (<code>{requester.id}</code>)\n"
            f"🎯 Looked up: {name} — <code>{uid}</code>",
        )
    elif hasattr(origin, "chat") and origin.chat is not None:
        cid = origin.chat.id
        name = origin.chat.title or origin.chat.full_name
        text = build_id_message(context.bot.username, "channel", cid, name=name)
        await msg.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=id_reply_markup(context.bot.username, cid, "channel", name),
        )
        await send_log(
            context,
            "🔎 <b>User search</b> (forwarded channel)\n"
            f"👤 By: {requester.full_name} (<code>{requester.id}</code>)\n"
            f"🎯 Looked up: {name} — <code>{cid}</code>",
        )
    else:
        sender_name = getattr(origin, "sender_user_name", None)
        await msg.reply_text(
            f"👤 Forwarded From : {sender_name or 'Hidden User'}\n\n"
            "🆔 ID : Not available (user has hidden their account)."
        )


async def new_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if context.bot.id in [member.id for member in update.message.new_chat_members]:
        text = build_id_message(context.bot.username, "chat", chat.id, name=chat.title)
        await context.bot.send_message(
            chat_id=chat.id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=id_reply_markup(context.bot.username, chat.id, "chat", chat.title),
        )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Cancelled.",
        reply_markup=ReplyKeyboardRemove(),
    )


# --------------------------------------------------------------------------
# Admin panel: stats, force-join management, global broadcast
# --------------------------------------------------------------------------
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def admin_panel_markup() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("📊 Stats", callback_data="admin_stats")],
        [InlineKeyboardButton("📋 Force-Join List", callback_data="admin_fj_list")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")],
    ]
    return InlineKeyboardMarkup(keyboard)


ADMIN_PANEL_TEXT = (
    "🛠 *Admin Panel*\n\n"
    "Use the buttons below, or these commands:\n"
    "• `/addforcejoin @channel` — add a force-join channel/group "
    "(bot must be admin there)\n"
    "• `/removeforcejoin @channel` — remove a force-join channel/group\n"
    "• Reply to any message with `/broadcast` — send it to every user, "
    "group & channel the bot knows about"
)


def broadcast_confirm_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Confirm & Send", callback_data="admin_broadcast_confirm"),
                InlineKeyboardButton("❌ Cancel", callback_data="admin_broadcast_cancel"),
            ]
        ]
    )


def broadcast_wait_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Cancel", callback_data="admin_broadcast_cancel")]]
    )


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("🚫 You're not authorized to use this command.")
        return
    await update.message.reply_text(
        ADMIN_PANEL_TEXT, parse_mode=ParseMode.MARKDOWN, reply_markup=admin_panel_markup()
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("🚫 You're not authorized to use this command.")
        return
    stats = get_stats()
    text = (
        "📊 *Bot Stats*\n\n"
        f"👤 Users: {stats['users']}\n"
        f"👥 Groups: {stats['groups']}\n"
        f"📢 Channels: {stats['channels']}\n"
        f"🔒 Force-Join Channels: {stats['force_channels']}\n"
        f"🧮 Total reach: {stats['users'] + stats['groups'] + stats['channels']}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def add_force_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("🚫 You're not authorized to use this command.")
        return
    if not context.args:
        await update.message.reply_text(
            "Usage:\n"
            "/addforcejoin @channelusername — for public channels/groups\n"
            "/addforcejoin -100xxxxxxxxxx — for private channels/groups (use the chat ID)\n\n"
            "(the bot must already be an admin there, with 'Invite Users via Link' "
            "permission for private channels)"
        )
        return
    identifier = context.args[0]
    try:
        try:
            identifier = int(identifier)
        except ValueError:
            pass
        chat = await context.bot.get_chat(identifier)
    except TelegramError as e:
        await update.message.reply_text(f"❌ Couldn't find that chat: {e}")
        return
    if any(c["chat_id"] == chat.id for c in STORE["force_channels"]):
        await update.message.reply_text("⚠️ Already in the force-join list.")
        return

    invite_link = None
    if not chat.username:
        invite_link = chat.invite_link
        if not invite_link:
            try:
                invite_link = (await context.bot.create_chat_invite_link(chat.id)).invite_link
            except TelegramError as e:
                await update.message.reply_text(
                    f"⚠️ Couldn't create an invite link for this private chat: {e}\n"
                    "Make sure the bot is an admin there with 'Invite Users via Link' permission."
                )
                return

    STORE["force_channels"].append(
        {
            "chat_id": chat.id,
            "username": chat.username,
            "title": chat.title or chat.full_name,
            "invite_link": invite_link,
        }
    )
    save_store()
    await update.message.reply_text(
        f"✅ Added {chat.title or chat.username} to the force-join list"
        f"{' (private, invite link generated)' if invite_link else ''}."
    )


async def remove_force_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("🚫 You're not authorized to use this command.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /removeforcejoin @channelusername")
        return
    identifier = context.args[0]
    target_id = None
    try:
        chat = await context.bot.get_chat(identifier)
        target_id = chat.id
    except TelegramError:
        try:
            target_id = int(identifier)
        except ValueError:
            await update.message.reply_text("❌ Couldn't resolve that chat.")
            return
    before = len(STORE["force_channels"])
    STORE["force_channels"] = [c for c in STORE["force_channels"] if c["chat_id"] != target_id]
    if len(STORE["force_channels"]) == before:
        await update.message.reply_text("⚠️ Not found in the force-join list.")
        return
    save_store()
    await update.message.reply_text("✅ Removed from the force-join list.")


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("🚫 You're not authorized to use this command.")
        return
    source = update.message.reply_to_message
    if not source:
        await update.message.reply_text(
            "↩️ Reply to the message you want to broadcast with /broadcast.\n"
            "It will be sent to every user, group & channel the bot knows about.\n\n"
            "Or tap 📢 Broadcast in /admin instead."
        )
        return

    context.user_data["awaiting_broadcast"] = False
    context.user_data["broadcast_source"] = {
        "chat_id": source.chat_id,
        "message_id": source.message_id,
    }
    targets = list(dict.fromkeys(STORE.get("users", []) + STORE.get("groups", []) + STORE.get("channels", [])))
    await update.message.reply_text(
        f"📢 Ready to broadcast this message to *{len(targets)}* chats "
        "(users + groups + channels). Confirm?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=broadcast_confirm_markup(),
    )


async def run_broadcast(context: ContextTypes.DEFAULT_TYPE, source: dict, edit_fn) -> None:
    targets = list(dict.fromkeys(STORE.get("users", []) + STORE.get("groups", []) + STORE.get("channels", [])))
    sent = 0
    failed = 0
    for chat_id in targets:
        try:
            await context.bot.copy_message(
                chat_id=chat_id,
                from_chat_id=source["chat_id"],
                message_id=source["message_id"],
            )
            sent += 1
        except TelegramError:
            failed += 1
            untrack_chat(chat_id)
        await asyncio.sleep(0.05)

    await edit_fn(
        f"✅ *Broadcast complete*\n\nSent: {sent}\nFailed/removed: {failed}",
    )


async def broadcast_await_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Captures the next message an admin sends after tapping 📢 Broadcast in /admin."""
    user = update.effective_user
    if not is_admin(user.id) or not context.user_data.get("awaiting_broadcast"):
        return
    context.user_data["awaiting_broadcast"] = False
    context.user_data["broadcast_source"] = {
        "chat_id": update.message.chat_id,
        "message_id": update.message.message_id,
    }
    targets = list(dict.fromkeys(STORE.get("users", []) + STORE.get("groups", []) + STORE.get("channels", [])))
    await update.message.reply_text(
        f"📢 Ready to broadcast this message to *{len(targets)}* chats "
        "(users + groups + channels). Confirm?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=broadcast_confirm_markup(),
    )


async def render_force_join_list(query: CallbackQuery) -> None:
    channels = STORE.get("force_channels", [])
    if not channels:
        await query.edit_message_text(
            "📋 No force-join channels set.\n\nAdd one with /addforcejoin @channelusername",
            reply_markup=admin_panel_markup(),
        )
        return
    buttons = []
    for ch in channels:
        label = ch.get("title") or ch.get("username") or str(ch["chat_id"])
        buttons.append(
            [InlineKeyboardButton(f"➖ Remove {label}", callback_data=f"admin_fj_rm:{ch['chat_id']}")]
        )
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="admin_back")])
    await query.edit_message_text(
        "📋 *Force-Join List* — tap to remove:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user
    if not is_admin(user.id):
        await query.answer("🚫 Not authorized.", show_alert=True)
        return
    await query.answer()

    if query.data == "admin_stats":
        stats = get_stats()
        text = (
            "📊 *Bot Stats*\n\n"
            f"👤 Users: {stats['users']}\n"
            f"👥 Groups: {stats['groups']}\n"
            f"📢 Channels: {stats['channels']}\n"
            f"🔒 Force-Join Channels: {stats['force_channels']}\n"
            f"🧮 Total reach: {stats['users'] + stats['groups'] + stats['channels']}"
        )
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=admin_panel_markup())
    elif query.data == "admin_fj_list":
        await render_force_join_list(query)
    elif query.data.startswith("admin_fj_rm:"):
        chat_id = int(query.data.split(":", 1)[1])
        STORE["force_channels"] = [c for c in STORE["force_channels"] if c["chat_id"] != chat_id]
        save_store()
        await render_force_join_list(query)
    elif query.data == "admin_back":
        context.user_data["awaiting_broadcast"] = False
        context.user_data.pop("broadcast_source", None)
        await query.edit_message_text(
            ADMIN_PANEL_TEXT, parse_mode=ParseMode.MARKDOWN, reply_markup=admin_panel_markup()
        )
    elif query.data == "admin_broadcast":
        context.user_data["awaiting_broadcast"] = True
        context.user_data.pop("broadcast_source", None)
        await query.edit_message_text(
            "📢 Send (or forward) the message you want to broadcast to every user, "
            "group & channel the bot knows about.\n\nTap ❌ Cancel to abort.",
            reply_markup=broadcast_wait_markup(),
        )
    elif query.data == "admin_broadcast_confirm":
        source = context.user_data.get("broadcast_source")
        if not source:
            await query.edit_message_text(
                "⚠️ Nothing to broadcast — it may have expired. Start again from the admin panel.",
                reply_markup=admin_panel_markup(),
            )
            return
        await query.edit_message_text("📢 Broadcasting...")

        async def _edit(text: str) -> None:
            await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)

        await run_broadcast(context, source, _edit)
        context.user_data["awaiting_broadcast"] = False
        context.user_data.pop("broadcast_source", None)
        await send_log(
            context,
            f"📢 <b>Broadcast sent</b> by {user.full_name} (<code>{user.id}</code>)",
        )
    elif query.data == "admin_broadcast_cancel":
        context.user_data["awaiting_broadcast"] = False
        context.user_data.pop("broadcast_source", None)
        await query.edit_message_text("❌ Broadcast cancelled.", reply_markup=admin_panel_markup())


def main() -> None:
    # Ensure BOT_TOKEN is set
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable is not set. "
            "Please set it in Render Dashboard > Environment Variables."
        )

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(MessageHandler(filters.ALL, track_chat_update), group=-1)
    application.add_handler(ChatMemberHandler(my_chat_member_update, ChatMemberHandler.MY_CHAT_MEMBER))

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("id", my_id))
    application.add_handler(CommandHandler("cancel", cancel))

    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(MessageHandler(filters.Text(["🛠 Admin Panel"]), admin_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("addforcejoin", add_force_join))
    application.add_handler(CommandHandler("removeforcejoin", remove_force_join))
    application.add_handler(CommandHandler("broadcast", broadcast_command))

    application.add_handler(CallbackQueryHandler(check_join_callback, pattern="^check_join$"))
    application.add_handler(CallbackQueryHandler(noop_callback, pattern="^noop$"))
    application.add_handler(CallbackQueryHandler(admin_callback, pattern="^admin_"))

    application.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, broadcast_await_message),
        group=2,
    )

    application.add_handler(MessageHandler(filters.StatusUpdate.USERS_SHARED, users_shared))
    application.add_handler(MessageHandler(filters.StatusUpdate.CHAT_SHARED, chat_shared))
    application.add_handler(MessageHandler(filters.FORWARDED, forwarded_message))
    application.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_chat_id)
    )

    logger.info("Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

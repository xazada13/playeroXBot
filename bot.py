"""Личный Telegram-бот помощник продавца на Playerok.

Функции: уведомления, авто-ответы (4 сценария), авто-поднятие,
авто-восстановление, управление объявлениями из Telegram.
Работает только для владельца (OWNER_ID).
"""
import asyncio
import html
import inspect
import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from dotenv import load_dotenv
from PlayerokAPI import Account, EventType, UnauthorizedError

load_dotenv()
TG_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
PL_TOKEN = os.getenv("PLAYEROK_TOKEN", "").strip()
SETTINGS_FILE = Path("settings.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("bot.log", encoding="utf-8")],
)
log = logging.getLogger("plbot")

# ---------------------------------------------------------------- настройки
DEFAULTS = {
    "autoreply": {
        "paid": {"on": True, "text": "Здравствуйте, спасибо за покупку! Скоро выдам вам заказ."},
        "sent": {"on": False, "text": "Заказ выдан. Пожалуйста, подтвердите получение и оставьте отзыв."},
        "confirmed": {"on": False, "text": "Спасибо за подтверждение! Буду рад вашему отзыву."},
        "problem": {"on": False, "text": "Вижу, что возникла проблема с заказом. Напишите, что случилось, и я всё решу."},
    },
    "notify": {"deal": True, "message": True},
    "items": {},
}
LABELS = {
    "paid": "💳 После оплаты",
    "sent": "📦 После вашей выдачи",
    "confirmed": "🤝 После подтверждения покупателем",
    "problem": "⚠️ При проблеме или возврате",
}


def merge(base: dict, extra: dict) -> dict:
    out = {}
    for k, v in base.items():
        if isinstance(v, dict) and not v:
            out[k] = extra.get(k, {}) if isinstance(extra.get(k), dict) else {}
        elif isinstance(v, dict):
            out[k] = merge(v, extra.get(k, {}) if isinstance(extra.get(k), dict) else {})
        else:
            out[k] = extra.get(k, v)
    return out


def load_settings() -> dict:
    data = {}
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text("utf-8"))
        except Exception:
            log.exception("settings.json повреждён, беру значения по умолчанию")
    return merge(DEFAULTS, data)


def save_settings() -> None:
    SETTINGS_FILE.write_text(json.dumps(S, ensure_ascii=False, indent=2), "utf-8")


S = load_settings()
acc: Account | None = None
bot: Bot | None = None
ME_ID = None
SEEN: dict = {}
ITEMS_CACHE: list = []


# ---------------------------------------------------------------- утилиты
def esc(x) -> str:
    return html.escape(str(x))


def g(obj, *names, default=None):
    """Безопасно достаёт первое существующее поле объекта."""
    for n in names:
        if obj is None:
            break
        v = obj.get(n) if isinstance(obj, dict) else getattr(obj, n, None)
        if v is not None:
            return v
    return default


def chat_link(chat_id) -> str:
    if not chat_id:
        return ""
    return f'<a href="https://playerok.com/chats/{esc(chat_id)}">Открыть чат</a>'


async def notify(text: str, chat_id=None) -> None:
    if not (bot and OWNER_ID):
        return
    rows = []
    if chat_id:
        rows.append([InlineKeyboardButton(text="💬 Открыть чат", url=f"https://playerok.com/chats/{chat_id}")])
    rows.append([InlineKeyboardButton(text="🗑 Скрыть", callback_data="x:hide")])
    try:
        await bot.send_message(OWNER_ID, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                               disable_web_page_preview=True)
    except Exception:
        log.exception("Не удалось отправить уведомление в Telegram")


# ------------------------------------------------- действия с объявлениями
# Названия методов объявлений в библиотеке я не мог проверить заранее,
# поэтому пробую несколько вариантов. Команда /diag покажет реальные.
async def _try(obj, attempts):
    errors = []
    for name, args, kwargs in attempts:
        fn = getattr(obj, name, None)
        if not callable(fn):
            continue
        try:
            res = fn(*args, **kwargs)
            return await res if inspect.isawaitable(res) else res
        except Exception as e:  # ошибка сайта или параметров: пробуем следующий вариант
            errors.append(f"{name}: {e}")
    raise NotImplementedError("; ".join(errors) or "подходящий метод не найден")


async def list_my_items() -> list:
    uid = ME_ID
    res = await _try(acc.items, [
        ("search", (), {"filter": {"userId": uid, "status": ["APPROVED"]}, "first": 50}),
        ("search", (), {"filter": {"userId": uid}, "first": 50}),
        ("list_self", (), {"limit": 50}),
        ("my", (), {}),
        ("mine", (), {}),
        ("own", (), {}),
    ])
    return list(res or [])


async def my_items():
    """Список объявлений. Если сайт не отдаёт общий список, берём добавленные по ссылке."""
    try:
        items = await list_my_items()
        if items:
            return items, False
    except Exception:
        log.exception("Общий список объявлений недоступен")
    saved = [{"id": r.get("id"), "name": r.get("name") or k, "price": "?"}
             for k, r in S["items"].items() if r.get("id")]
    return saved, True


async def bump_item(item_id):
    return await _try(acc.items, [
        ("bump", (item_id,), {}),
        ("raise_up", (item_id,), {}),
        ("up", (item_id,), {}),
        ("promote", (item_id,), {}),
        ("boost", (item_id,), {}),
        ("increase_priority", (item_id,), {}),
    ])


async def restore_item(item_id):
    return await _try(acc.items, [
        ("restore", (item_id,), {}),
        ("republish", (item_id,), {}),
        ("relist", (item_id,), {}),
        ("publish", (item_id,), {}),
    ])


async def delete_item(item_id):
    return await _try(acc.items, [
        ("remove", (item_id,), {}),
        ("delete", (item_id,), {}),
    ])


# ----------------------------------------------------------------- события
def status_key(deal) -> str:
    raw = g(deal, "status", "state")
    name = str(getattr(raw, "name", raw) or "").upper()
    if "CONFIRM" in name:
        return "confirmed"
    if "SENT" in name:
        return "sent"
    if any(w in name for w in ("ROLL", "REFUND", "DISPUT", "PROBLEM")):
        return "problem"
    if "PAID" in name:
        return "paid"
    return ""


def deal_chat_id(deal):
    return g(deal, "chat_id") or g(g(deal, "chat"), "id")


CHAT_META: dict = {}  # chat_id -> {"buyer": ..., "item": ...}


def deal_buyer(deal) -> str:
    u = g(deal, "user", "buyer", "counterparty", "customer", "participant")
    return str(g(u, "username", "name", default="") or "")


def deal_item_name(deal) -> str:
    return str(g(g(deal, "item"), "name", default="") or "")


DEALS_BY_CHAT: dict = {}
DEALS_BY_BUYER: dict = {}
LAST_DUMPS: dict = {}
SYSTEM_RE = re.compile(r"^\s*\{\{\s*([A-Za-z0-9_]+)\s*\}\}\s*$")


def remember_chat(deal) -> None:
    chat_id = deal_chat_id(deal)
    buyer, item = deal_buyer(deal), deal_item_name(deal)
    if chat_id:
        CHAT_META[chat_id] = {"buyer": buyer, "item": item}
        DEALS_BY_CHAT[chat_id] = deal
    if buyer:
        DEALS_BY_BUYER[buyer] = deal


def msg_chat_id(msg, event=None):
    """Номер чата сообщения: в разных версиях он лежит в разных полях."""
    for src in (msg, event):
        v = g(src, "chat_id", "chatId")
        if v:
            return v
        c = g(src, "chat")
        if c is not None:
            if isinstance(c, (str, int)):
                return c
            v = g(c, "id")
            if v:
                return v
    return None


def msg_author_name(msg) -> str:
    return str(g(g(msg, "user", "author", "sender"), "username", "name", default="") or "")


def describe(obj, depth: int = 2) -> str:
    """Короткое описание объекта (для диагностики)."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return repr(obj)[:80]
    if depth <= 0:
        return f"<{type(obj).__name__}>"
    if isinstance(obj, (list, tuple, set)):
        return "[" + ", ".join(describe(x, depth - 1) for x in list(obj)[:3]) + "]"
    if isinstance(obj, dict):
        pairs = list(obj.items())
    else:
        pairs = []
        for n in dir(obj):
            if n.startswith("_"):
                continue
            try:
                v = getattr(obj, n)
            except Exception:
                continue
            if not callable(v):
                pairs.append((n, v))
    return f"{type(obj).__name__}(" + ", ".join(f"{k}={describe(v, depth - 1)}" for k, v in pairs) + ")"


async def chat_meta(chat_id) -> dict:
    """Кто пишет и по какому объявлению (из прошлых сделок или из чата на сайте)."""
    if not chat_id:
        return {}
    meta = dict(CHAT_META.get(chat_id) or {})
    if meta.get("buyer") and meta.get("item"):
        return meta
    try:
        chat = await _try(acc.chats, [
            ("get", (chat_id,), {}),
            ("get_chat", (chat_id,), {}),
            ("by_id", (chat_id,), {}),
        ])
        deal = g(chat, "deal")
        item = g(g(deal, "item"), "name") or g(g(chat, "item"), "name")
        if item and not meta.get("item"):
            meta["item"] = str(item)
        if not meta.get("buyer"):
            for u in list(g(chat, "users", "participants", default=[]) or []):
                if g(u, "id") != ME_ID and g(u, "username"):
                    meta["buyer"] = str(g(u, "username"))
                    break
        if not meta.get("buyer"):
            meta["buyer"] = deal_buyer(deal)
    except Exception as e:
        log.warning("Не удалось получить данные чата %s: %s", chat_id, e)
    CHAT_META[chat_id] = meta
    return meta


def who_what(buyer: str, item: str) -> str:
    return f"👤 Покупатель: <b>{esc(buyer or 'не определён')}</b>\n📦 Объявление: {esc(item or 'не определено')}"


def fmt_text(template: str, deal) -> str:
    info = {
        "buyer": deal_buyer(deal) or "покупатель",
        "item": deal_item_name(deal) or "товар",
        "deal_id": g(deal, "id", default=""),
    }

    class Safe(dict):
        def __missing__(self, k):
            return "{" + k + "}"

    return template.format_map(Safe(info))


def find_item_rule(deal):
    """Ищет настроенные тексты для объявления, к которому относится сделка."""
    it = g(deal, "item")
    ids = {str(x) for x in (g(it, "id"), g(deal, "item_id")) if x}
    slugs = {str(g(it, "slug"))} if g(it, "slug") else set()
    name = str(g(it, "name", default="")).strip().lower()
    for r in S["items"].values():
        if r.get("id") and str(r["id"]) in ids:
            return r
        if r["slug"] in slugs:
            return r
        if name and r.get("name", "").strip().lower() == name:
            return r
    return None


def parse_slug(text: str) -> str:
    text = text.strip()
    path = urlparse(text).path if "://" in text or text.startswith("playerok") else text
    parts = [x for x in path.split("/") if x]
    return parts[-1] if parts else ""


def sent_status():
    """Значение статуса «выдано» из перечислений библиотеки."""
    try:
        from PlayerokAPI import enums
    except Exception:
        return None
    for n in dir(enums):
        cls = getattr(enums, n)
        if isinstance(cls, type) and hasattr(cls, "SENT"):
            return cls.SENT
    return None


def ns_methods(ns) -> list:
    out = []
    for n in sorted(dir(ns)):
        if n.startswith("_") or not callable(getattr(ns, n, None)):
            continue
        try:
            out.append(f"{n}{inspect.signature(getattr(ns, n))}")
        except (TypeError, ValueError):
            out.append(f"{n}()")
    return out


def build_kwargs(fn, deal_id, status):
    """Подбирает аргументы метода по именам параметров. None, если не подходит."""
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return None
    kwargs, got_id = {}, False
    for p in params:
        if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        n = p.name.lower()
        if n in ("changes", "data", "fields", "values", "payload", "input", "params"):
            kwargs[p.name] = {"status": status}  # update(deal_id, changes={"status": ...})
        elif "status" in n:
            kwargs[p.name] = status
        elif n in ("id", "deal", "deal_id", "dealid") or n.endswith("_id"):
            kwargs[p.name] = deal_id
            got_id = True
        elif p.default is inspect.Parameter.empty:
            return None
    return kwargs if got_id else None


async def confirm_deal(deal) -> str:
    """Отмечает заказ выданным (подтверждение выполнения продавцом).
    Нужный метод ищется в acc.deals по смыслу названия. Возвращает имя сработавшего метода."""
    deal_id = g(deal, "id")
    raw = g(deal, "status", "state")
    own = getattr(type(raw), "SENT", None) if raw is not None and not isinstance(raw, str) else None
    statuses = [x for x in ("SENT", own, sent_status()) if x is not None]
    ns = acc.deals
    good = ("status", "update", "complete", "sent", "fulfil", "deliver", "mark", "finish")
    bad = ("rollback", "refund", "cancel", "problem", "create", "buy", "pay", "report",
           "delete", "remove", "list", "search", "get", "find", "fetch", "confirm")
    names = [n for n in dir(ns)
             if not n.startswith("_") and callable(getattr(ns, n, None))
             and any(w in n.lower() for w in good) and not any(w in n.lower() for w in bad)]
    names.sort(key=lambda n: (0 if "status" in n.lower() else 1, n))
    errors = []
    for n in names:
        fn = getattr(ns, n)
        for st in statuses:
            kwargs = build_kwargs(fn, deal_id, st)
            if kwargs is None:
                continue
            try:
                res = fn(**kwargs)
                if inspect.isawaitable(res):
                    await res
                return n
            except Exception as e:
                errors.append(f"{n}: {str(e)[:120]}")
    raise RuntimeError(
        "Подходящий метод не сработал.\n"
        f"Пробовал: {', '.join(names) or 'ничего не нашёл'}\n"
        f"Ошибки: {' | '.join(errors[:4]) or '-'}\n"
        f"Методы acc.deals: {'; '.join(ns_methods(ns))[:900]}")


TITLES = {
    "paid": "💳 Новый оплаченный заказ",
    "sent": "📦 Заказ отмечен выданным",
    "confirmed": "🤝 Покупатель подтвердил заказ",
    "problem": "⚠️ Проблема, спор или возврат",
}


async def on_deal(deal, key: str) -> None:
    deal_id = g(deal, "id")
    chat_id = deal_chat_id(deal)
    buyer, item = deal_buyer(deal), deal_item_name(deal)
    mark = (deal_id or chat_id or buyer, key)
    if mark in SEEN:
        return
    SEEN[mark] = time.time()
    if len(SEEN) > 2000:
        for k in sorted(SEEN, key=SEEN.get)[:500]:
            SEEN.pop(k, None)
    if deal_id:
        remember_chat(deal)

    custom = find_item_rule(deal)
    if S["notify"]["deal"]:
        extra = "\n⚙️ Авто-подтверждение включено" if key == "paid" and custom and custom.get("auto_confirm") else ""
        await notify(f"<b>{TITLES[key]}</b>\n{who_what(buyer, item)}{extra}", chat_id=chat_id)

    reply_text = ""
    if custom and custom["texts"].get(key, "").strip():
        reply_text = custom["texts"][key]
    else:
        rule = S["autoreply"].get(key)
        if rule and rule["on"] and rule["text"].strip():
            reply_text = rule["text"]
    if reply_text:
        if chat_id:
            try:
                await acc.send_message(chat_id, fmt_text(reply_text, deal))
            except Exception:
                log.exception("Не удалось отправить автоответ")
                await notify("❗ Не удалось отправить автоответ покупателю. Проверьте bot.log")
        else:
            await notify("❗ Не смог определить чат сделки для автоответа. Отправьте мне /events.")

    if key == "paid" and custom and custom.get("auto_confirm"):
        if not deal_id:
            await notify("❗ Авто-подтверждение не выполнено: не нашёл номер заказа.")
        else:
            try:
                method = await confirm_deal(deal)
                log.info("Авто-подтверждение выполнено методом %s", method)
                await notify(f"✅ <b>Авто-подтверждение выполнено</b>\n{who_what(buyer, item)}")
            except Exception as e:
                log.exception("Авто-подтверждение не удалось")
                await notify(f"❗ <b>Авто-подтверждение не удалось</b>\n{esc(item)}\n\n{esc(str(e)[:1500])}\n\n"
                             "Подтвердите заказ вручную и перешлите это сообщение мне.")


async def on_system(token: str, msg, event, chat_id) -> None:
    """Системные сообщения Playerok вида {{DEAL_CONFIRMED}} (проблема, подтверждение и т.д.)."""
    buyer = msg_author_name(msg)
    deal = (DEALS_BY_CHAT.get(chat_id) if chat_id else None) or DEALS_BY_BUYER.get(buyer)
    if deal is None:
        meta = await chat_meta(chat_id)
        buyer = buyer or meta.get("buyer", "")
        deal = {"id": None, "chat_id": chat_id, "item": {"name": meta.get("item", "")}, "user": {"username": buyer}}
    cid = chat_id or deal_chat_id(deal)

    if "PROBLEM_RESOLVED" in token:
        SEEN.pop((g(deal, "id") or cid or buyer, "problem"), None)
        if S["notify"]["deal"]:
            await notify(f"✅ <b>Проблема решена</b>\n{who_what(deal_buyer(deal) or buyer, deal_item_name(deal))}", chat_id=cid)
        return
    if "HAS_PROBLEM" in token or "ROLLED_BACK" in token or "REFUND" in token:
        key = "problem"
    elif "CONFIRMED" in token:
        key = "confirmed"
    elif "ITEM_PAID" in token or "ITEM_SENT" in token:
        return  # эти статусы обрабатываются событиями сделок
    else:
        if S["notify"]["deal"]:
            await notify(f"ℹ️ <b>Системное событие:</b> {esc(token)}\n{who_what(deal_buyer(deal) or buyer, deal_item_name(deal))}", chat_id=cid)
        return
    await on_deal(deal, key)


async def on_message(event) -> None:
    msg = g(event, "message")
    if msg is None:
        return
    text = str(g(msg, "text") or "")
    chat_id = msg_chat_id(msg, event)
    m = SYSTEM_RE.match(text)
    if m:
        await on_system(m.group(1).upper(), msg, event, chat_id)
        return
    if g(msg, "is_system") or g(msg, "author_id") == ME_ID or not S["notify"]["message"]:
        return
    meta = await chat_meta(chat_id)
    buyer = meta.get("buyer") or msg_author_name(msg)
    item = meta.get("item") or (deal_item_name(DEALS_BY_BUYER[buyer]) if buyer in DEALS_BY_BUYER else "")
    await notify(f"💬 <b>Новое сообщение</b>\n{who_what(buyer, item)}\n\n"
                 f"✉️ {esc((text or '[вложение]')[:500])}", chat_id=chat_id)


async def handle_event(event) -> None:
    try:
        t = g(event, "type")
        if t in (EventType.NEW_MESSAGE, EventType.NEW_DEAL, EventType.DEAL_UPDATED):
            LAST_DUMPS[str(getattr(t, "name", t))] = describe(event, 3)
        if t == EventType.NEW_MESSAGE:
            await on_message(event)
        elif t == EventType.NEW_DEAL:
            deal = g(event, "deal")
            await on_deal(deal, status_key(deal) or "paid")
        elif t == EventType.DEAL_UPDATED:
            deal = g(event, "deal")
            key = status_key(deal)
            if key:
                await on_deal(deal, key)
    except Exception:
        log.exception("Ошибка обработки события")


EVENTS = [EventType.NEW_MESSAGE, EventType.NEW_DEAL, EventType.DEAL_UPDATED]


async def events_loop() -> None:
    fails = 0
    while True:
        try:
            if fails < 3:
                listener = acc.listener(events=EVENTS)
                for t in EVENTS:
                    listener.on(t)(handle_event)
                log.info("Слушаю события через WebSocket")
                await listener.run()
                raise RuntimeError("слушатель остановился")
            log.info("Перешёл на поллинг (раз в 5 секунд)")
            async for ev in acc.polling(interval=5.0).events():
                await handle_event(ev)
            raise RuntimeError("поллинг остановился")
        except asyncio.CancelledError:
            raise
        except UnauthorizedError:
            await notify("❗ Токен Playerok устарел. Запустите login.py и перезапустите бота.")
            await asyncio.sleep(600)
        except Exception as e:
            fails += 1
            log.exception("Сбой цикла событий (%s)", fails)
            await asyncio.sleep(min(60, 5 * fails))


# ---------------------------------------------------------------- Telegram
dp = Dispatcher()
PANEL = {"mid": None}  # id единственного сообщения-панели, которое редактируется на месте


def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
    ])


def onoff(v: bool) -> str:
    return "🟢 ВКЛ" if v else "⚪️ ВЫКЛ"


MAIN_KB = kb([
    [("📦 Объявления", "m:items")],
    [("🎯 Сценарии по объявлениям", "m:sc")],
    [("💬 Общие авто-ответы", "m:ar")],
    [("🔔 Уведомления", "m:notify"), ("👤 Аккаунт", "m:acc")],
])
BACK = [("‹ Главное меню", "m:main")]
HIDE_KB = kb([[("🗑 Скрыть", "x:hide")]])
MAIN_TEXT = "⚡ <b>Главное меню</b>"
SHORT = {"paid": "Оплата", "sent": "Выдача", "confirmed": "Подтверждение", "problem": "Проблема"}


async def del_msg(m) -> None:
    """Удаляет сообщение пользователя, чтобы чат оставался чистым."""
    try:
        await m.delete()
    except Exception:
        pass


async def panel_edit(text: str, markup=None) -> None:
    """Обновляет сообщение-панель на месте. Если его нет, создаёт заново."""
    if PANEL["mid"]:
        try:
            await bot.edit_message_text(text, chat_id=OWNER_ID, message_id=PANEL["mid"],
                                        reply_markup=markup, disable_web_page_preview=True)
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return
    msg = await bot.send_message(OWNER_ID, text, reply_markup=markup, disable_web_page_preview=True)
    PANEL["mid"] = msg.message_id


async def send_panel(text: str = MAIN_TEXT) -> None:
    """Новая панель внизу чата; старая удаляется."""
    if PANEL["mid"]:
        try:
            await bot.delete_message(OWNER_ID, PANEL["mid"])
        except Exception:
            pass
    msg = await bot.send_message(OWNER_ID, text, reply_markup=MAIN_KB)
    PANEL["mid"] = msg.message_id


async def show(q: CallbackQuery, text: str, markup=None) -> None:
    PANEL["mid"] = q.message.message_id  # панелью считается сообщение, на которое нажали
    await panel_edit(text, markup)


def ar_screen():
    lines = ["<b>💬 Общие авто-ответы</b>\n<i>Работают для всех объявлений, где не задан свой текст.</i>\n"]
    rows = []
    for key, label in LABELS.items():
        r = S["autoreply"][key]
        lines.append(f"{label} — {onoff(r['on'])}\n<i>{esc(r['text'])}</i>\n")
        rows.append([(f"{'🟢' if r['on'] else '⚪️'} {SHORT[key]}", f"ar:t:{key}"), ("✏️ Текст", f"ar:e:{key}")])
    lines.append("Переменные: {buyer}, {item}, {deal_id}")
    rows.append(BACK)
    return "\n".join(lines), kb(rows)


def notify_screen():
    n = S["notify"]
    names = {"deal": "Заказы и статусы", "message": "Сообщения"}
    text = "<b>🔔 Уведомления</b>\n" + "\n".join(f"{names[k]} — {onoff(n[k])}" for k in names)
    rows = [[(f"{names[k]}: {'выключить' if n[k] else 'включить'}", f"nf:t:{k}")] for k in names]
    return text, kb(rows + [BACK])


async def items_screen():
    global ITEMS_CACHE
    items, fallback = await my_items()
    ITEMS_CACHE = items[:8]
    if not ITEMS_CACHE:
        return ("<b>📦 Объявления</b>\nСайт не отдал список объявлений.\n\n"
                "Добавьте объявления по ссылке: «🎯 Сценарии по объявлениям» → «➕ Добавить по ссылке». "
                "После этого они появятся здесь, и кнопки заработают.", kb([BACK]))
    lines, rows = ["<b>📦 Ваши объявления</b>\n"], []
    if fallback:
        lines.append("<i>Показываю объявления, добавленные по ссылке.</i>\n")
    for i, it in enumerate(ITEMS_CACHE):
        price = g(it, "price", default="?")
        tail = f" — {esc(price)} ₽" if str(price) != "?" else ""
        lines.append(f"{i + 1}. {esc(g(it, 'name', default='без названия'))}{tail}")
        rows.append([(f"⬆️ {i + 1}", f"i:b:{i}"), (f"♻️ {i + 1}", f"i:r:{i}"), (f"🗑 {i + 1}", f"i:d:{i}")])
    lines.append("\n⬆️ поднять  ♻️ восстановить  🗑 удалить")
    return "\n".join(lines), kb(rows + [BACK])


def owner_only(m) -> bool:
    return bool(OWNER_ID) and m.from_user.id == OWNER_ID


class Form(StatesGroup):
    link = State()
    text = State()
    gtext = State()


# ----- команды (все, кроме /id, работают только у владельца)
@dp.message(Command("id"))
async def cmd_id(m: Message):
    # показывает человеку только его собственный ID, ничего более
    await m.answer(f"Ваш Telegram ID: <code>{m.from_user.id}</code>\nВпишите его в .env (OWNER_ID) и перезапустите бота.")


@dp.message(CommandStart(), owner_only)
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await del_msg(m)
    await send_panel()


@dp.message(Command("cancel"), owner_only)
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    await del_msg(m)
    await panel_edit(MAIN_TEXT, MAIN_KB)


@dp.message(Command("settext"), owner_only)
async def cmd_settext(m: Message):
    parts = (m.text or "").split(maxsplit=2)
    await del_msg(m)
    if len(parts) == 3 and parts[1] in S["autoreply"]:
        S["autoreply"][parts[1]]["text"] = parts[2]
        save_settings()
    await panel_edit(*ar_screen())


@dp.message(Command("events"), owner_only)
async def cmd_events(m: Message):
    await del_msg(m)
    if not LAST_DUMPS:
        await bot.send_message(OWNER_ID, "Пока не было ни одного события. Совершите тестовую покупку и повторите.", reply_markup=HIDE_KB)
        return
    text = "\n\n".join(f"[{k}]\n{v}" for k, v in LAST_DUMPS.items())
    for i in range(0, len(text), 3500):
        await bot.send_message(OWNER_ID, f"<pre>{esc(text[i:i + 3500])}</pre>", reply_markup=HIDE_KB)


@dp.message(Command("diag"), owner_only)
async def cmd_diag(m: Message):
    await del_msg(m)
    out = [f"Playerok ID: {ME_ID}"]
    for ns_name in ("items", "deals", "chats", "testimonials"):
        out.append(f"\n[{ns_name}]")
        out += ns_methods(getattr(acc, ns_name, None))
    text = "\n".join(out)
    for i in range(0, len(text), 3500):
        await bot.send_message(OWNER_ID, f"<pre>{esc(text[i:i + 3500])}</pre>", reply_markup=HIDE_KB)


# ----- кнопки
@dp.callback_query(F.data == "x:hide", owner_only)
async def cb_hide(q: CallbackQuery):
    await del_msg(q.message)
    await q.answer()


@dp.callback_query(F.data.startswith("m:"), owner_only)
async def cb_menu(q: CallbackQuery, state: FSMContext):
    await state.clear()
    key = q.data[2:]
    try:
        if key == "main":
            await show(q, MAIN_TEXT, MAIN_KB)
        elif key == "sc":
            await show(q, *sc_list_screen())
        elif key == "ar":
            await show(q, *ar_screen())
        elif key == "notify":
            await show(q, *notify_screen())
        elif key == "items":
            await show(q, *(await items_screen()))
        elif key == "acc":
            me = await acc.get_me()
            await show(q, f"<b>👤 Аккаунт</b>\nПользователь: {esc(me.username)}\n"
                          f"Баланс: {esc(me.balance.available)} ₽", kb([BACK]))
    except Exception as e:
        log.exception("Ошибка меню")
        await show(q, f"❗ Ошибка: {esc(str(e)[:300])}", kb([BACK]))
    await q.answer()


@dp.callback_query(F.data.startswith("ar:t:"), owner_only)
async def cb_ar(q: CallbackQuery):
    key = q.data.split(":")[2]
    S["autoreply"][key]["on"] = not S["autoreply"][key]["on"]
    save_settings()
    await show(q, *ar_screen())
    await q.answer()


@dp.callback_query(F.data.startswith("ar:e:"), owner_only)
async def cb_ar_edit(q: CallbackQuery, state: FSMContext):
    key = q.data.split(":")[2]
    await state.set_state(Form.gtext)
    await state.update_data(key=key)
    await show(q, f"✏️ <b>Общий текст: {LABELS[key]}</b>\n\nСейчас:\n<i>{esc(S['autoreply'][key]['text'])}</i>\n\n"
                  "Пришлите новый текст одним сообщением.\nПеременные: {buyer}, {item}, {deal_id}",
               kb([[("‹ Отмена", "m:ar")]]))
    await q.answer()


@dp.message(Form.gtext, F.text, ~F.text.startswith("/"), owner_only)
async def got_gtext(m: Message, state: FSMContext):
    data = await state.get_data()
    S["autoreply"][data["key"]]["text"] = m.text
    save_settings()
    await state.clear()
    await del_msg(m)
    await panel_edit(*ar_screen())


@dp.callback_query(F.data.startswith("nf:t:"), owner_only)
async def cb_nf(q: CallbackQuery):
    key = q.data.split(":")[2]
    S["notify"][key] = not S["notify"][key]
    save_settings()
    await show(q, *notify_screen())
    await q.answer()


@dp.callback_query(F.data.startswith("i:"), owner_only)
async def cb_item(q: CallbackQuery):
    _, action, idx = q.data.split(":")
    idx = int(idx)
    if idx >= len(ITEMS_CACHE):
        await q.answer("Список устарел, откройте «Объявления» заново", show_alert=True)
        return
    it = ITEMS_CACHE[idx]
    item_id, name = g(it, "id"), g(it, "name", default="объявление")
    back = kb([[("‹ К объявлениям", "m:items")]])
    try:
        if action == "b":
            await bump_item(item_id)
            await q.answer("⬆️ Поднято")
        elif action == "r":
            await restore_item(item_id)
            await q.answer("♻️ Восстановлено")
        elif action == "d":
            await show(q, f"Удалить «{esc(name)}»?", kb([[("🗑 Да, удалить", f"i:x:{idx}"), ("Отмена", "m:items")]]))
            await q.answer()
        elif action == "x":
            await delete_item(item_id)
            await q.answer("🗑 Удалено")
            await show(q, *(await items_screen()))
    except Exception as e:
        log.exception("Действие с объявлением")
        await show(q, f"❗ Не получилось: {esc(str(e)[:600])}\n\nЕсли повторяется, отправьте боту /diag и перешлите ответ мне.", back)
        await q.answer()


# ------------------------------------------------ сценарии по объявлениям
def item_keys() -> list:
    return list(S["items"].keys())


def sc_list_screen():
    keys = item_keys()
    text = ("<b>🎯 Сценарии по объявлениям</b>\n\n"
            "Для каждого объявления можно задать свои тексты. Если текст не задан, "
            "используется общий авто-ответ.\n")
    text += "\n".join(f"{i + 1}. {esc(S['items'][k].get('name') or k)}" for i, k in enumerate(keys)) \
        if keys else "\nПока ни одного объявления не добавлено."
    rows = [[(f"{'⚙️ ' if S['items'][k].get('auto_confirm') else ''}{i + 1}. {(S['items'][k].get('name') or k)[:36]}", f"sc:o:{i}")]
            for i, k in enumerate(keys)]
    rows.append([("➕ Добавить по ссылке", "sc:add")])
    rows.append(BACK)
    return text, kb(rows)


def sc_item_screen(i: int):
    key = item_keys()[i]
    r = S["items"][key]
    ac = bool(r.get("auto_confirm"))
    lines = [f"<b>🎯 {esc(r.get('name') or key)}</b>\n",
             f"⚙️ Авто-подтверждение: {onoff(ac)}\n"
             "<i>Сразу после оплаты бот сам отметит заказ выданным (для товаров с автовыдачей Playerok).</i>\n"]
    for k, label in LABELS.items():
        t = r["texts"].get(k, "").strip()
        lines.append(f"{label}\n<i>{esc(t) if t else 'общий текст'}</i>\n")
    lines.append("Переменные: {buyer}, {item}, {deal_id}")
    rows = [[(f"⚙️ Авто-подтверждение: {'выключить' if ac else 'включить'}", f"sc:ac:{i}")]]
    rows += [[(f"✏️ {label}", f"sc:e:{i}:{k}")] for k, label in LABELS.items()]
    rows.append([("🗑 Убрать объявление", f"sc:del:{i}")])
    rows.append([("‹ К списку", "m:sc")])
    return "\n".join(lines), kb(rows)


@dp.callback_query(F.data == "sc:add", owner_only)
async def cb_sc_add(q: CallbackQuery, state: FSMContext):
    await state.set_state(Form.link)
    await show(q, "➕ <b>Новое объявление</b>\n\nПришлите ссылку на объявление Playerok\n"
                  "(например, https://playerok.com/products/...).",
               kb([[("‹ Отмена", "m:sc")]]))
    await q.answer()


@dp.message(Form.link, F.text, ~F.text.startswith("/"), owner_only)
async def got_link(m: Message, state: FSMContext):
    slug = parse_slug(m.text)
    await del_msg(m)
    if not slug:
        await panel_edit("⚠️ Не вижу ссылки. Пришлите ссылку на объявление.", kb([[("‹ Отмена", "m:sc")]]))
        return
    name, item_id, warn = slug, None, ""
    try:
        item = await acc.items.get(slug=slug)
        name = g(item, "name", default=slug)
        item_id = g(item, "id")
    except Exception:
        log.exception("Не удалось получить объявление по ссылке")
        warn = "⚠️ Не смог загрузить объявление с сайта, сохранил по ссылке.\n\n"
    if slug not in S["items"]:
        S["items"][slug] = {"slug": slug, "id": item_id, "name": str(name), "texts": {}, "auto_confirm": False}
    else:
        S["items"][slug].update({"id": item_id or S["items"][slug].get("id"), "name": str(name)})
    save_settings()
    await state.clear()
    text, markup = sc_item_screen(item_keys().index(slug))
    await panel_edit(f"✅ Добавлено\n\n{warn}{text}", markup)


@dp.callback_query(F.data.startswith("sc:o:"), owner_only)
async def cb_sc_open(q: CallbackQuery, state: FSMContext):
    await state.clear()
    i = int(q.data.split(":")[2])
    if i >= len(item_keys()):
        await q.answer("Список изменился, откройте заново", show_alert=True)
        return
    await show(q, *sc_item_screen(i))
    await q.answer()


@dp.callback_query(F.data.startswith("sc:e:"), owner_only)
async def cb_sc_edit(q: CallbackQuery, state: FSMContext):
    _, _, i, key = q.data.split(":")
    keys = item_keys()
    if int(i) >= len(keys):
        await q.answer("Список изменился, откройте заново", show_alert=True)
        return
    r = S["items"][keys[int(i)]]
    cur = r["texts"].get(key, "").strip()
    await state.set_state(Form.text)
    await state.update_data(i=int(i), key=key)
    await show(q, f"✏️ <b>{LABELS[key]}</b>\nОбъявление: {esc(r.get('name') or keys[int(i)])}\n\n"
                  f"Сейчас: <i>{esc(cur) if cur else 'общий текст'}</i>\n\n"
                  "Пришлите новый текст одним сообщением.\n«-» вернёт общий текст.\n"
                  "Переменные: {buyer}, {item}, {deal_id}",
               kb([[("‹ Отмена", f"sc:o:{i}")]]))
    await q.answer()


@dp.message(Form.text, F.text, ~F.text.startswith("/"), owner_only)
async def got_text(m: Message, state: FSMContext):
    data = await state.get_data()
    keys = item_keys()
    await del_msg(m)
    await state.clear()
    if data["i"] >= len(keys):
        await panel_edit("Список изменился, откройте объявление заново.", kb([[("‹ К списку", "m:sc")]]))
        return
    texts = S["items"][keys[data["i"]]]["texts"]
    if m.text.strip() == "-":
        texts.pop(data["key"], None)
    else:
        texts[data["key"]] = m.text
    save_settings()
    await panel_edit(*sc_item_screen(data["i"]))


@dp.callback_query(F.data.startswith("sc:ac:"), owner_only)
async def cb_sc_ac(q: CallbackQuery):
    i = int(q.data.split(":")[2])
    keys = item_keys()
    if i >= len(keys):
        await q.answer("Список изменился, откройте заново", show_alert=True)
        return
    r = S["items"][keys[i]]
    r["auto_confirm"] = not r.get("auto_confirm")
    save_settings()
    await show(q, *sc_item_screen(i))
    await q.answer("Авто-подтверждение включено" if r["auto_confirm"] else "Выключено")


@dp.callback_query(F.data.startswith("sc:del:"), owner_only)
async def cb_sc_del(q: CallbackQuery):
    i = int(q.data.split(":")[2])
    keys = item_keys()
    if i < len(keys):
        S["items"].pop(keys[i], None)
        save_settings()
    await show(q, *sc_list_screen())
    await q.answer("Убрано")


# любое другое сообщение владельца удаляем, чтобы в чате не копился мусор
@dp.message(owner_only)
async def cleanup_any(m: Message):
    await del_msg(m)


# -------------------------------------------------------------------- запуск
async def main() -> None:
    global acc, bot, ME_ID
    if not TG_TOKEN:
        raise SystemExit("Не задан TG_BOT_TOKEN в файле .env")
    if not PL_TOKEN:
        raise SystemExit("Нет токена Playerok. Сначала запустите: python login.py")

    bot = Bot(TG_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp.message.filter(F.chat.type == "private")
    async with Account(token=PL_TOKEN) as a:
        acc = a
        try:
            me = await acc.get_me()
        except UnauthorizedError:
            raise SystemExit("Токен Playerok устарел. Запустите: python login.py")
        ME_ID = me.id
        log.info("Вход выполнен: %s", me.username)
        if OWNER_ID:
            await send_panel(f"✅ Бот запущен · <b>{esc(me.username)}</b>\n\n{MAIN_TEXT}")
        else:
            log.warning("OWNER_ID не задан: напишите боту /id и впишите число в .env")
        tasks = [asyncio.create_task(events_loop())]
        try:
            await dp.start_polling(bot)
        finally:
            for t in tasks:
                t.cancel()
            await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

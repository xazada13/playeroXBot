"""Личный Telegram-бот помощник продавца на Playerok (финальная версия).

Возможности:
  - уведомления о заказах и сообщениях с кнопками «Открыть чат», «Ответить», «Скрыть»;
  - авто-ответы: общие и свои для каждого объявления (4 сценария);
  - авто-подтверждение выдачи для выбранных объявлений;
  - много объявлений: короткие названия, страницы, поиск, массовые действия, копирование настроек;
  - порядок в чате: одно сообщение-меню, автоудаление ваших сообщений и старых уведомлений,
    сообщения одного покупателя собираются в одно уведомление, кнопка «Очистить»;
  - статистика за день.
Работает только для владельца (OWNER_ID).
"""
import asyncio
import html
import inspect
import json
import logging
import logging.handlers
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
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
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler("bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"),
    ],
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
    "notify": {"deal": True, "message": True, "silent_msgs": False, "msg_ttl_h": 24},
    "items": {},
    "stats": {},
}
LABELS = {
    "paid": "💳 После оплаты",
    "sent": "📦 После вашей выдачи",
    "confirmed": "🤝 После подтверждения покупателем",
    "problem": "⚠️ При проблеме или возврате",
}
SHORT = {"paid": "Оплата", "sent": "Выдача", "confirmed": "Подтверждение", "problem": "Проблема"}


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
    """Сохраняет атомарно: сначала во временный файл, потом подменяет, чтобы файл не повредился."""
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(S, ensure_ascii=False, indent=2), "utf-8")
    os.replace(tmp, SETTINGS_FILE)


S = load_settings()
acc: Account | None = None
bot: Bot | None = None
ME_ID = None
SEEN: dict = {}

NOTIFS: dict = {}        # id сообщения-уведомления -> {ts, kind, buyer, chat, reply}
BUYER_NOTIF: dict = {}   # покупатель -> id его актуального уведомления о сообщениях
BUYER_COUNT: dict = {}
BUYER_TEXTS: dict = {}
DEAL_NOTIF: dict = {}    # сделка -> (id уведомления, базовый текст)
AUTO_DONE: set = set()   # сделки, которые подтверждает сам бот


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


MSK = timezone(timedelta(hours=3))


def today_key() -> str:
    return datetime.now(MSK).strftime("%Y-%m-%d")


# ----------------------------------------------------- уведомления (с учётом)
def notif_markup(chat_id=None, reply=False) -> InlineKeyboardMarkup:
    rows = []
    if chat_id:
        row = [InlineKeyboardButton(text="💬 Открыть чат", url=f"https://playerok.com/chats/{chat_id}")]
        if reply:
            row.append(InlineKeyboardButton(text="✍️ Ответить", callback_data=f"rp:{chat_id}"))
        rows.append(row)
    rows.append([InlineKeyboardButton(text="🗑 Скрыть", callback_data="x:hide")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def notify(text: str, chat_id=None, kind: str = "deal", buyer: str = "",
                 reply: bool = False, silent: bool = False):
    """Отправляет уведомление владельцу и запоминает его (для очистки). Возвращает id сообщения."""
    if not (bot and OWNER_ID):
        return None
    try:
        m = await bot.send_message(OWNER_ID, text, reply_markup=notif_markup(chat_id, reply),
                                   disable_web_page_preview=True, disable_notification=silent)
    except Exception:
        log.exception("Не удалось отправить уведомление в Telegram")
        return None
    NOTIFS[m.message_id] = {"ts": time.time(), "kind": kind, "buyer": buyer, "chat": chat_id, "reply": reply}
    return m.message_id


async def edit_notif(mid, text: str) -> bool:
    info = NOTIFS.get(mid)
    if not info:
        return False
    try:
        await bot.edit_message_text(text, chat_id=OWNER_ID, message_id=mid,
                                    reply_markup=notif_markup(info["chat"], info["reply"]),
                                    disable_web_page_preview=True)
        return True
    except TelegramBadRequest as e:
        return "not modified" in str(e)
    except Exception:
        return False


def forget_notif(mid, reset: bool = True) -> None:
    info = NOTIFS.pop(mid, None)
    if info and info.get("buyer") and BUYER_NOTIF.get(info["buyer"]) == mid:
        BUYER_NOTIF.pop(info["buyer"], None)
        if reset:
            BUYER_COUNT.pop(info["buyer"], None)
            BUYER_TEXTS.pop(info["buyer"], None)


async def delete_notif(mid, reset: bool = True) -> None:
    forget_notif(mid, reset)
    try:
        await bot.delete_message(OWNER_ID, mid)
    except Exception:
        pass


async def clear_notifs() -> int:
    n = 0
    for mid in list(NOTIFS):
        await delete_notif(mid)
        n += 1
        await asyncio.sleep(0.05)
    return n


# ------------------------------------------------- действия с объявлениями
# Названия методов объявлений в библиотеке заранее неизвестны, поэтому пробую несколько
# вариантов. Команда /diag покажет реальные.
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


CHAT_BY_DEAL: dict = {}   # deal_id -> chat_id (когда чат появился позже сделки)
BUYER_CHAT: dict = {}     # имя покупателя -> chat_id
PENDING: dict = {}        # deal_id -> тексты, которые отправим, когда появится чат


def deal_chat_id(deal):
    did = g(deal, "id")
    return (g(deal, "chat_id") or g(g(deal, "chat"), "id")
            or g(g(g(deal, "raw"), "chat"), "id")
            or (CHAT_BY_DEAL.get(did) if did else None))


async def resolve_chat_id(deal_id, tries: int = 3, delay: float = 1.2):
    """У новой сделки чат создаётся с задержкой: запрашиваем сделку заново, пока он не появится."""
    for i in range(tries):
        try:
            d = await acc.deals.get(deal_id)
            cid = g(d, "chat_id") or g(g(g(d, "raw"), "chat"), "id") or g(g(d, "chat"), "id")
            if cid:
                CHAT_BY_DEAL[deal_id] = cid
                return cid
        except Exception as e:
            log.warning("Не удалось запросить сделку %s: %s", deal_id, e)
        await asyncio.sleep(delay)
    return None


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
            BUYER_CHAT[buyer] = chat_id
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
    """Подставляет {buyer}, {item}, {deal_id}. Остальные скобки остаются как есть."""
    info = {
        "buyer": deal_buyer(deal) or "покупатель",
        "item": deal_item_name(deal) or "товар",
        "deal_id": str(g(deal, "id", default="")),
    }
    return re.sub(r"\{(buyer|item|deal_id)\}", lambda m: info[m.group(1)], template)


def find_item_rule(deal):
    """Ищет настроенные тексты для объявления, к которому относится сделка."""
    it = g(deal, "item")
    ids = {str(x) for x in (g(it, "id"), g(deal, "item_id")) if x}
    slugs = {str(g(it, "slug"))} if g(it, "slug") else set()
    url_part = parse_slug(str(g(it, "url", default="") or ""))
    if url_part:
        slugs.add(url_part)
        ids.add(url_part)
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


def bump_stats(deal) -> None:
    """Считает заказ и сумму (после комиссии) в статистике за сегодня."""
    value = g(g(deal, "transaction"), "value") or g(g(deal, "item"), "price") or 0
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = 0.0
    st = S.setdefault("stats", {})
    day = st.setdefault(today_key(), {"orders": 0, "sum": 0})
    day["orders"] += 1
    day["sum"] = round(day["sum"] + value, 2)
    for k in sorted(st)[:-30]:
        st.pop(k, None)
    save_settings()


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
    if not chat_id and deal_id and key == "paid":
        chat_id = await resolve_chat_id(deal_id)
    if deal_id:
        remember_chat(deal)
    if chat_id and deal_id and deal_id in PENDING:
        for t in PENDING.pop(deal_id):
            try:
                await acc.send_message(chat_id, t)
            except Exception:
                log.exception("Не удалось отправить отложенный ответ")
    if key == "paid":
        bump_stats(deal)

    custom = find_item_rule(deal)
    auto = bool(key == "paid" and custom and custom.get("auto_confirm") and deal_id)
    if auto:
        AUTO_DONE.add(deal_id)  # заранее: событие «выдано» не должно давать лишнее уведомление
    base = f"<b>{TITLES[key]}</b>\n{who_what(buyer, item)}"
    if S["notify"]["deal"] and not (key == "sent" and deal_id in AUTO_DONE):
        mid = await notify(base + ("\n⚙️ Авто-подтверждение…" if auto else ""), chat_id=chat_id)
        if auto and mid:
            DEAL_NOTIF[deal_id] = (mid, base)

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
        elif deal_id:
            PENDING.setdefault(deal_id, []).append(fmt_text(reply_text, deal))
            await notify("⏳ Чат с покупателем ещё не создан. Отправлю автоответ, как только он появится.")
        else:
            await notify("❗ Не смог определить чат сделки для автоответа. Отправьте мне /events.")

    if key == "paid" and custom and custom.get("auto_confirm"):
        if not deal_id:
            await notify("❗ Авто-подтверждение не выполнено: не нашёл номер заказа.")
            return
        try:
            method = await confirm_deal(deal)
            log.info("Авто-подтверждение выполнено методом %s", method)
            if S["notify"]["deal"]:
                saved = DEAL_NOTIF.pop(deal_id, None)
                done = "\n✅ <b>Выдача подтверждена автоматически</b>"
                if not (saved and await edit_notif(saved[0], saved[1] + done)):
                    await notify(f"✅ <b>Заказ подтверждён автоматически</b>\n{who_what(buyer, item)}", chat_id=chat_id)
        except Exception as e:
            AUTO_DONE.discard(deal_id)
            log.exception("Авто-подтверждение не удалось")
            await notify(f"❗ <b>Авто-подтверждение не удалось</b>\n{esc(item)}\n\n{esc(str(e)[:1500])}\n\n"
                         "Подтвердите заказ вручную и перешлите это сообщение мне.")


async def on_system(token: str, msg, event, chat_id, notify_unknown: bool = True) -> None:
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
        if notify_unknown and S["notify"]["deal"]:
            await notify(f"ℹ️ <b>Системное событие:</b> {esc(token)}\n{who_what(deal_buyer(deal) or buyer, deal_item_name(deal))}", chat_id=cid)
        return
    await on_deal(deal, key)


async def on_message(event) -> None:
    msg = g(event, "message")
    if msg is None:
        return
    text = str(g(msg, "text") or "")
    chat_id = msg_chat_id(msg, event) or BUYER_CHAT.get(msg_author_name(msg))
    m = SYSTEM_RE.match(text)
    if m:
        await on_system(m.group(1).upper(), msg, event, chat_id)
        return
    ev = g(msg, "event")
    if ev is not None and g(msg, "is_system"):
        await on_system(str(getattr(ev, "name", ev)).upper(), msg, event, chat_id, notify_unknown=False)
        return
    if g(msg, "is_system") or g(msg, "author_id") == ME_ID or not S["notify"]["message"]:
        return
    meta = await chat_meta(chat_id)
    buyer = meta.get("buyer") or msg_author_name(msg)
    item = meta.get("item") or (deal_item_name(DEALS_BY_BUYER[buyer]) if buyer in DEALS_BY_BUYER else "")

    # сообщения одного покупателя собираем в одно уведомление
    key = buyer or str(chat_id or "?")
    texts = BUYER_TEXTS.setdefault(key, [])
    texts.append((text or "[вложение]")[:300])
    del texts[:-3]
    BUYER_COUNT[key] = BUYER_COUNT.get(key, 0) + 1
    count = BUYER_COUNT[key]
    old = BUYER_NOTIF.get(key)
    if old:
        await delete_notif(old, reset=False)
    head = "💬 <b>Новое сообщение</b>" if count == 1 else f"💬 <b>Новые сообщения · {count}</b>"
    body = "\n".join(f"✉️ {esc(t)}" for t in texts)
    mid = await notify(f"{head}\n{who_what(buyer, item)}\n\n{body}", chat_id=chat_id, kind="msg",
                       buyer=key, reply=True, silent=bool(S["notify"].get("silent_msgs")))
    if mid:
        BUYER_NOTIF[key] = mid


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
    fails, dead_at = 0, 0.0
    while True:
        t0 = time.time()
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
            if time.time() - dead_at > 6 * 3600:  # не спамим одним и тем же
                dead_at = time.time()
                await notify("❗ Токен Playerok устарел. Возьмите новый токен, обновите PLAYEROK_TOKEN в .env и перезапустите бота.")
            await asyncio.sleep(600)
        except Exception:
            if time.time() - t0 > 600:
                fails = 0  # до сбоя работало долго, это не постоянная проблема
            fails += 1
            log.exception("Сбой цикла событий (%s)", fails)
            await asyncio.sleep(min(60, 5 * fails))


async def cleanup_loop() -> None:
    """Автоудаление старых уведомлений о сообщениях (срок задаётся в «Уведомления»)."""
    while True:
        await asyncio.sleep(300)
        ttl = S["notify"].get("msg_ttl_h", 0)
        if not ttl:
            continue
        limit = time.time() - ttl * 3600
        for mid, info in list(NOTIFS.items()):
            if info["kind"] == "msg" and info["ts"] < limit:
                await delete_notif(mid)
                await asyncio.sleep(0.1)


# ---------------------------------------------------------------- Telegram
dp = Dispatcher()
PANEL = {"mid": None}   # id единственного сообщения-панели, которое редактируется на месте
SC = {"q": ""}          # текущий поисковый запрос в списке объявлений
PAGE = 8                # объявлений на странице


def kb(rows) -> InlineKeyboardMarkup:
    """rows: список рядов кнопок (текст, данные). Данные http... = кнопка-ссылка."""
    def btn(t, d):
        if str(d).startswith("http"):
            return InlineKeyboardButton(text=t, url=d)
        return InlineKeyboardButton(text=t, callback_data=d)
    return InlineKeyboardMarkup(inline_keyboard=[[btn(t, d) for t, d in row] for row in rows])


def onoff(v: bool) -> str:
    return "🟢 ВКЛ" if v else "⚪️ ВЫКЛ"


BACK = [("‹ Главное меню", "m:main")]


def main_text() -> str:
    st = S.get("stats", {}).get(today_key(), {"orders": 0, "sum": 0})
    return (f"⚡ <b>Главное меню</b>\n\n📊 Сегодня: <b>{st['orders']}</b> зак. · <b>+{st['sum']:g} ₽</b>\n"
            f"💬 Диалогов с новыми сообщениями: {len(BUYER_NOTIF)}")


def main_kb() -> InlineKeyboardMarkup:
    n = len(NOTIFS)
    return kb([
        [("🎯 Сценарии по объявлениям", "m:sc")],
        [("💬 Общие авто-ответы", "m:ar")],
        [("🔔 Уведомления", "m:notify"), (f"🧹 Очистить ({n})" if n else "🧹 Очистить", "m:clear")],
        [("👤 Аккаунт", "m:acc")],
    ])


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


async def send_panel(text: str = "") -> None:
    """Новая панель внизу чата; старая удаляется."""
    if PANEL["mid"]:
        try:
            await bot.delete_message(OWNER_ID, PANEL["mid"])
        except Exception:
            pass
    msg = await bot.send_message(OWNER_ID, text or main_text(), reply_markup=main_kb())
    PANEL["mid"] = msg.message_id


async def show(q: CallbackQuery, text: str, markup=None) -> None:
    PANEL["mid"] = q.message.message_id  # панелью считается сообщение, на которое нажали
    await panel_edit(text, markup)


def owner_only(m) -> bool:
    return bool(OWNER_ID) and m.from_user.id == OWNER_ID


class Form(StatesGroup):
    link = State()
    text = State()
    gtext = State()
    alias = State()
    search = State()
    reply = State()


# ------------------------------------------------------------- общие экраны
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


TTL_CYCLE = [0, 6, 24, 72]


def notify_screen():
    n = S["notify"]
    ttl = n.get("msg_ttl_h", 0)
    text = ("<b>🔔 Уведомления</b>\n\n"
            f"Заказы и статусы — {onoff(n['deal'])}\n"
            f"Сообщения — {onoff(n['message'])}\n"
            f"Звук у сообщений — {onoff(not n.get('silent_msgs'))}\n"
            f"Автоудаление сообщений — {'выключено' if not ttl else f'через {ttl} ч'}")
    rows = [
        [(f"Заказы: {'выключить' if n['deal'] else 'включить'}", "nf:t:deal")],
        [(f"Сообщения: {'выключить' if n['message'] else 'включить'}", "nf:t:message")],
        [(f"Звук у сообщений: {'выключить' if not n.get('silent_msgs') else 'включить'}", "nf:t:silent_msgs")],
        [("⏱ Автоудаление: сменить срок", "nf:ttl")],
        BACK,
    ]
    return text, kb(rows)


# ------------------------------------------------ сценарии по объявлениям
def short_alias(name: str) -> str:
    """Короткое название из длинного: без эмодзи и значков, первые слова."""
    words = re.sub(r"[^\w\s\-+]", " ", name or "").split()
    alias = " ".join(words[:3])[:24].strip()
    return alias or "Объявление"


def alias_of(r: dict) -> str:
    return r.get("alias") or short_alias(r.get("name") or r.get("slug", ""))


def icons(r: dict) -> str:
    s = "⚙️" if r.get("auto_confirm") else ""
    if any(str(t).strip() for t in r.get("texts", {}).values()):
        s += "✏️"
    return s


def item_keys() -> list:
    return list(S["items"].keys())


def filtered_keys() -> list:
    q = SC["q"].lower()
    return [k for k in item_keys()
            if not q or q in (alias_of(S["items"][k]) + " " + (S["items"][k].get("name") or "")).lower()]


def sc_list_screen(page: int = 0):
    keys = filtered_keys()
    total = max(1, (len(keys) + PAGE - 1) // PAGE)
    page = min(max(page, 0), total - 1)
    chunk = keys[page * PAGE:(page + 1) * PAGE]
    all_keys = item_keys()
    head = "<b>🎯 Сценарии по объявлениям</b>"
    if SC["q"]:
        head += f"\n🔎 Поиск: «{esc(SC['q'])}», найдено {len(keys)}"
    lines = [head, "<i>⚙️ авто-подтверждение · ✏️ свои тексты</i>\n"]
    if not keys:
        lines.append("Ничего не найдено." if SC["q"] else "Пока ни одного объявления. Нажмите «➕ Добавить».")
    btns = []
    for k in chunk:
        r = S["items"][k]
        lines.append(f"{icons(r) or '▫️'} <b>{esc(alias_of(r))}</b> · {esc((r.get('name') or k)[:36])}")
        btns.append((f"{icons(r)} {alias_of(r)[:16]}".strip(), f"sc:o:{all_keys.index(k)}"))
    rows = [btns[j:j + 2] for j in range(0, len(btns), 2)]
    if total > 1:
        rows.append([("‹", f"sc:p:{page - 1}") if page > 0 else ("·", "noop"),
                     (f"{page + 1}/{total}", "noop"),
                     ("›", f"sc:p:{page + 1}") if page < total - 1 else ("·", "noop")])
    rows.append([("🔎 Поиск", "sc:find"), ("➕ Добавить", "sc:add")])
    extra = [("⚙️ Массово", "sc:mass")]
    if SC["q"]:
        extra.append(("✖️ Сбросить поиск", "sc:clr"))
    rows.append(extra)
    rows.append(BACK)
    return "\n".join(lines), kb(rows)


def sc_item_screen(i: int):
    key = item_keys()[i]
    r = S["items"][key]
    ac = bool(r.get("auto_confirm"))
    lines = [f"<b>🎯 {esc(alias_of(r))}</b>", f"<i>{esc(r.get('name') or key)}</i>\n",
             f"⚙️ Авто-подтверждение: {onoff(ac)}\n"]
    for k, label in LABELS.items():
        t = r["texts"].get(k, "").strip()
        shown = (esc(t[:120]) + ("…" if len(t) > 120 else "")) if t else "общий текст"
        lines.append(f"{label}\n<i>{shown}</i>\n")
    lines.append("Переменные: {buyer}, {item}, {deal_id}")
    rows = [[(f"⚙️ Авто-подтверждение: {'выключить' if ac else 'включить'}", f"sc:ac:{i}")]]
    ed = [(f"✏️ {SHORT[k]}", f"sc:e:{i}:{k}") for k in LABELS]
    rows += [ed[0:2], ed[2:4]]
    rows += [[("✏️ Название", f"sc:rn:{i}"), ("📋 Копировать из…", f"sc:cp:{i}")],
             [("🔧 Управление", f"sc:mg:{i}"), ("🗑 Убрать", f"sc:del:{i}")],
             [("‹ К списку", "m:sc")]]
    return "\n".join(lines), kb(rows)


def sc_mass_screen():
    keys = filtered_keys()
    on = sum(1 for k in keys if S["items"][k].get("auto_confirm"))
    scope = "найденных" if SC["q"] else "всех"
    text = (f"<b>⚙️ Массовые действия</b>\nОбъявлений ({scope}): {len(keys)}, "
            f"с авто-подтверждением: {on}")
    return text, kb([[("🟢 Включить авто-подтверждение", "sc:mon")],
                     [("⚪️ Выключить авто-подтверждение", "sc:moff")],
                     [("‹ К списку", "m:sc")]])


def sc_copy_screen(i: int):
    keys = item_keys()
    btns = [(alias_of(S["items"][k])[:16], f"sc:cx:{i}:{j}") for j, k in enumerate(keys) if j != i][:40]
    rows = [btns[j:j + 2] for j in range(0, len(btns), 2)] + [[("‹ Назад", f"sc:o:{i}")]]
    return ("<b>📋 Копировать настройки</b>\nВыберите объявление, откуда взять тексты и "
            "авто-подтверждение. Текущие настройки будут заменены.", kb(rows))


def sc_manage_screen(i: int):
    r = S["items"][item_keys()[i]]
    return (f"<b>🔧 Управление: {esc(alias_of(r))}</b>\nДействия на сайте Playerok.",
            kb([[("⬆️ Поднять", f"sc:mb:{i}"), ("♻️ Восстановить", f"sc:mr:{i}")],
                [("🗑 Удалить с сайта", f"sc:md:{i}")],
                [("‹ Назад", f"sc:o:{i}")]]))


async def add_item_by_slug(slug: str) -> bool:
    """Добавляет объявление. Возвращает False, если не удалось загрузить его с сайта."""
    name, item_id, ok = slug, None, True
    try:
        item = await acc.items.get(slug=slug)
        name = g(item, "name", default=slug)
        item_id = g(item, "id")
    except Exception:
        log.exception("Не удалось получить объявление по ссылке")
        ok = False
    r = S["items"].get(slug)
    if r is None:
        S["items"][slug] = {"slug": slug, "id": item_id, "name": str(name),
                            "alias": short_alias(str(name)), "texts": {}, "auto_confirm": False}
    else:
        r.update({"id": item_id or r.get("id"), "name": str(name)})
    return ok


# ------------------------------------------------------------------ команды
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
    await panel_edit(main_text(), main_kb())


@dp.message(Command("clear"), owner_only)
async def cmd_clear(m: Message):
    await del_msg(m)
    await clear_notifs()
    await panel_edit(main_text(), main_kb())


@dp.message(Command("events"), owner_only)
async def cmd_events(m: Message):
    await del_msg(m)
    hide = kb([[("🗑 Скрыть", "x:hide")]])
    if not LAST_DUMPS:
        await bot.send_message(OWNER_ID, "Пока не было ни одного события. Совершите тестовую покупку и повторите.", reply_markup=hide)
        return
    text = "\n\n".join(f"[{k}]\n{v}" for k, v in LAST_DUMPS.items())
    for i in range(0, len(text), 3500):
        await bot.send_message(OWNER_ID, f"<pre>{esc(text[i:i + 3500])}</pre>", reply_markup=hide)


@dp.message(Command("diag"), owner_only)
async def cmd_diag(m: Message):
    await del_msg(m)
    out = [f"Playerok ID: {ME_ID}"]
    for ns_name in ("items", "deals", "chats", "testimonials"):
        out.append(f"\n[{ns_name}]")
        out += ns_methods(getattr(acc, ns_name, None))
    text = "\n".join(out)
    for i in range(0, len(text), 3500):
        await bot.send_message(OWNER_ID, f"<pre>{esc(text[i:i + 3500])}</pre>", reply_markup=kb([[("🗑 Скрыть", "x:hide")]]))


# ------------------------------------------------------------------- кнопки
@dp.callback_query(F.data == "noop", owner_only)
async def cb_noop(q: CallbackQuery):
    await q.answer()


@dp.callback_query(F.data == "x:hide", owner_only)
async def cb_hide(q: CallbackQuery):
    forget_notif(q.message.message_id)
    await del_msg(q.message)
    await q.answer()


@dp.callback_query(F.data.startswith("m:"), owner_only)
async def cb_menu(q: CallbackQuery, state: FSMContext):
    await state.clear()
    key = q.data[2:]
    try:
        if key == "main":
            await show(q, main_text(), main_kb())
        elif key == "sc":
            await show(q, *sc_list_screen())
        elif key == "ar":
            await show(q, *ar_screen())
        elif key == "notify":
            await show(q, *notify_screen())
        elif key == "clear":
            n = await clear_notifs()
            await show(q, main_text(), main_kb())
            await q.answer(f"Удалено уведомлений: {n}")
            return
        elif key == "acc":
            me = await acc.get_me()
            await show(q, f"<b>👤 Аккаунт</b>\nПользователь: {esc(me.username)}\n"
                          f"Баланс: {esc(me.balance.available)} ₽", kb([BACK]))
    except Exception as e:
        log.exception("Ошибка меню")
        await show(q, f"❗ Ошибка: {esc(str(e)[:300])}", kb([BACK]))
    await q.answer()


# ----- общие авто-ответы
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


# ----- уведомления
@dp.callback_query(F.data.startswith("nf:"), owner_only)
async def cb_nf(q: CallbackQuery):
    parts = q.data.split(":")
    if parts[1] == "ttl":
        cur = S["notify"].get("msg_ttl_h", 0)
        S["notify"]["msg_ttl_h"] = TTL_CYCLE[(TTL_CYCLE.index(cur) + 1) % len(TTL_CYCLE)] if cur in TTL_CYCLE else 24
    else:
        key = parts[2]
        S["notify"][key] = not S["notify"].get(key, False)
    save_settings()
    await show(q, *notify_screen())
    await q.answer()


# ----- быстрый ответ покупателю из уведомления
@dp.callback_query(F.data.startswith("rp:"), owner_only)
async def cb_reply(q: CallbackQuery, state: FSMContext):
    await state.set_state(Form.reply)
    await state.update_data(chat_id=q.data[3:])
    # панель, а не само уведомление, чтобы уведомление осталось на месте
    await panel_edit("✍️ <b>Ответ покупателю</b>\n\nПришлите текст: отправлю его в чат от вашего имени.",
                     kb([[("‹ Отмена", "m:main")]]))
    await q.answer()


@dp.message(Form.reply, F.text, ~F.text.startswith("/"), owner_only)
async def got_reply(m: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    await del_msg(m)
    try:
        await acc.send_message(data["chat_id"], m.text)
        await panel_edit("✅ Ответ отправлен\n\n" + main_text(), main_kb())
    except Exception as e:
        log.exception("Не удалось отправить быстрый ответ")
        await panel_edit(f"❗ Не удалось отправить: {esc(str(e)[:300])}", kb([BACK]))


# ----- сценарии: список, поиск, добавление
@dp.callback_query(F.data.startswith("sc:p:"), owner_only)
async def cb_sc_page(q: CallbackQuery):
    await show(q, *sc_list_screen(int(q.data.split(":")[2])))
    await q.answer()


@dp.callback_query(F.data == "sc:find", owner_only)
async def cb_sc_find(q: CallbackQuery, state: FSMContext):
    await state.set_state(Form.search)
    await show(q, "🔎 <b>Поиск объявления</b>\n\nПришлите слово из названия (например, «Claude»).",
               kb([[("‹ Отмена", "m:sc")]]))
    await q.answer()


@dp.message(Form.search, F.text, ~F.text.startswith("/"), owner_only)
async def got_search(m: Message, state: FSMContext):
    SC["q"] = m.text.strip()
    await state.clear()
    await del_msg(m)
    await panel_edit(*sc_list_screen())


@dp.callback_query(F.data == "sc:clr", owner_only)
async def cb_sc_clr(q: CallbackQuery):
    SC["q"] = ""
    await show(q, *sc_list_screen())
    await q.answer("Поиск сброшен")


@dp.callback_query(F.data == "sc:add", owner_only)
async def cb_sc_add(q: CallbackQuery, state: FSMContext):
    await state.set_state(Form.link)
    await show(q, "➕ <b>Новые объявления</b>\n\nПришлите ссылку на объявление Playerok. "
                  "Можно сразу несколько ссылок, каждую с новой строки.",
               kb([[("‹ Отмена", "m:sc")]]))
    await q.answer()


@dp.message(Form.link, F.text, ~F.text.startswith("/"), owner_only)
async def got_link(m: Message, state: FSMContext):
    raw = m.text
    await del_msg(m)
    found = re.findall(r"https?://\S+", raw) or [x for x in raw.splitlines() if x.strip()]
    slugs = []
    for u in found:
        s = parse_slug(u)
        if s and s not in slugs:
            slugs.append(s)
    slugs = slugs[:30]
    if not slugs:
        await panel_edit("⚠️ Не вижу ссылки. Пришлите ссылку на объявление.", kb([[("‹ Отмена", "m:sc")]]))
        return
    failed = 0
    for s in slugs:
        if not await add_item_by_slug(s):
            failed += 1
    save_settings()
    await state.clear()
    note = f"✅ Добавлено: {len(slugs)}"
    if failed:
        note += f"\n⚠️ Не загрузилось с сайта: {failed} (сохранены по ссылке)"
    if len(slugs) == 1:
        text, markup = sc_item_screen(item_keys().index(slugs[0]))
        await panel_edit(f"{note}\n\n{text}", markup)
    else:
        text, markup = sc_list_screen()
        await panel_edit(f"{note}\n\n{text}", markup)


# ----- сценарии: карточка объявления
@dp.callback_query(F.data.startswith("sc:o:"), owner_only)
async def cb_sc_open(q: CallbackQuery, state: FSMContext):
    await state.clear()
    i = int(q.data.split(":")[2])
    if i >= len(item_keys()):
        await q.answer("Список изменился, откройте заново", show_alert=True)
        return
    await show(q, *sc_item_screen(i))
    await q.answer()


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
    await show(q, f"✏️ <b>{LABELS[key]}</b>\nОбъявление: {esc(alias_of(r))}\n\n"
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


@dp.callback_query(F.data.startswith("sc:rn:"), owner_only)
async def cb_sc_rename(q: CallbackQuery, state: FSMContext):
    i = int(q.data.split(":")[2])
    keys = item_keys()
    if i >= len(keys):
        await q.answer("Список изменился, откройте заново", show_alert=True)
        return
    await state.set_state(Form.alias)
    await state.update_data(i=i)
    await show(q, f"✏️ <b>Короткое название</b>\nСейчас: {esc(alias_of(S['items'][keys[i]]))}\n\n"
                  "Пришлите новое название (до 30 символов). Оно показывается на кнопках.",
               kb([[("‹ Отмена", f"sc:o:{i}")]]))
    await q.answer()


@dp.message(Form.alias, F.text, ~F.text.startswith("/"), owner_only)
async def got_alias(m: Message, state: FSMContext):
    data = await state.get_data()
    keys = item_keys()
    await del_msg(m)
    await state.clear()
    if data["i"] < len(keys):
        S["items"][keys[data["i"]]]["alias"] = m.text.strip()[:30] or short_alias(S["items"][keys[data["i"]]].get("name", ""))
        save_settings()
        await panel_edit(*sc_item_screen(data["i"]))


@dp.callback_query(F.data.startswith("sc:cp:"), owner_only)
async def cb_sc_copy(q: CallbackQuery):
    i = int(q.data.split(":")[2])
    if i >= len(item_keys()):
        await q.answer("Список изменился, откройте заново", show_alert=True)
        return
    await show(q, *sc_copy_screen(i))
    await q.answer()


@dp.callback_query(F.data.startswith("sc:cx:"), owner_only)
async def cb_sc_copy_do(q: CallbackQuery):
    _, _, i, j = q.data.split(":")
    keys = item_keys()
    i, j = int(i), int(j)
    if i >= len(keys) or j >= len(keys):
        await q.answer("Список изменился, откройте заново", show_alert=True)
        return
    src, dst = S["items"][keys[j]], S["items"][keys[i]]
    dst["texts"] = dict(src.get("texts", {}))
    dst["auto_confirm"] = bool(src.get("auto_confirm"))
    save_settings()
    await show(q, *sc_item_screen(i))
    await q.answer("Настройки скопированы")


@dp.callback_query(F.data.startswith("sc:del:"), owner_only)
async def cb_sc_del(q: CallbackQuery):
    i = int(q.data.split(":")[2])
    keys = item_keys()
    if i < len(keys):
        S["items"].pop(keys[i], None)
        save_settings()
    await show(q, *sc_list_screen())
    await q.answer("Убрано из бота (на сайте не тронуто)")


# ----- массовые действия
@dp.callback_query(F.data == "sc:mass", owner_only)
async def cb_sc_mass(q: CallbackQuery):
    await show(q, *sc_mass_screen())
    await q.answer()


@dp.callback_query(F.data.in_({"sc:mon", "sc:moff"}), owner_only)
async def cb_sc_mass_do(q: CallbackQuery):
    value = q.data == "sc:mon"
    keys = filtered_keys()
    for k in keys:
        S["items"][k]["auto_confirm"] = value
    save_settings()
    await show(q, *sc_mass_screen())
    await q.answer(f"Готово: {len(keys)} шт.")


# ----- управление объявлением на сайте (поднять / восстановить / удалить)
@dp.callback_query(F.data.startswith("sc:mg:"), owner_only)
async def cb_sc_manage(q: CallbackQuery):
    i = int(q.data.split(":")[2])
    if i >= len(item_keys()):
        await q.answer("Список изменился, откройте заново", show_alert=True)
        return
    await show(q, *sc_manage_screen(i))
    await q.answer()


@dp.callback_query(F.data.regexp(r"^sc:(mb|mr|md|mx):\d+$"), owner_only)
async def cb_sc_site_action(q: CallbackQuery):
    _, action, i = q.data.split(":")
    i = int(i)
    keys = item_keys()
    if i >= len(keys):
        await q.answer("Список изменился, откройте заново", show_alert=True)
        return
    r = S["items"][keys[i]]
    item_id = r.get("id")
    back = kb([[("‹ Назад", f"sc:mg:{i}")]])
    if not item_id:
        await q.answer("Нет ID объявления. Уберите его и добавьте заново по ссылке.", show_alert=True)
        return
    try:
        if action == "mb":
            await bump_item(item_id)
            await q.answer("⬆️ Поднято")
        elif action == "mr":
            await restore_item(item_id)
            await q.answer("♻️ Восстановлено")
        elif action == "md":
            await show(q, f"Удалить «{esc(alias_of(r))}» с сайта Playerok? Это нельзя отменить.",
                       kb([[("🗑 Да, удалить", f"sc:mx:{i}"), ("Отмена", f"sc:mg:{i}")]]))
            await q.answer()
        elif action == "mx":
            await delete_item(item_id)
            S["items"].pop(keys[i], None)
            save_settings()
            await show(q, *sc_list_screen())
            await q.answer("🗑 Удалено")
    except Exception as e:
        log.exception("Действие с объявлением")
        await show(q, f"❗ Не получилось: {esc(str(e)[:600])}\n\nЕсли повторяется, отправьте боту /diag и перешлите ответ мне.", back)
        await q.answer()


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
        raise SystemExit("Нет токена Playerok. Задайте PLAYEROK_TOKEN в .env (или запустите login.py)")

    bot = Bot(TG_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp.message.filter(F.chat.type == "private")
    async with Account(token=PL_TOKEN) as a:
        acc = a
        try:
            me = await acc.get_me()
        except UnauthorizedError:
            raise SystemExit("Токен Playerok устарел. Возьмите новый токен и обновите .env")
        ME_ID = me.id
        log.info("Вход выполнен: %s", me.username)
        if OWNER_ID:
            await send_panel(f"✅ Бот запущен · <b>{esc(me.username)}</b>\n\n{main_text()}")
        else:
            log.warning("OWNER_ID не задан: напишите боту /id и впишите число в .env")
        tasks = [asyncio.create_task(events_loop()), asyncio.create_task(cleanup_loop())]
        try:
            await dp.start_polling(bot)
        finally:
            for t in tasks:
                t.cancel()
            await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

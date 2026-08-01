import asyncio
import io
import logging
from html import escape
from urllib.parse import quote, urlparse

import httpx
import qrcode
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import (BufferedInputFile, CallbackQuery, ErrorEvent, InlineKeyboardButton, InlineKeyboardMarkup,
                           LabeledPrice, Message, PreCheckoutQuery)

from app.config import get_settings
from app.services.broadcast_drafts import BroadcastDraftStore
from app.services.telegram_html import sanitize_telegram_html

logging.basicConfig(level=logging.INFO)
settings = get_settings()
dp = Dispatcher()
support_waiting: set[int] = set()
admin_reply_waiting: dict[int, str] = {}
admin_user_search_waiting: set[int] = set()
donation_custom_waiting: set[int] = set()
broadcast_drafts = BroadcastDraftStore()


def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📨 Поддержка", callback_data="adm:tickets"), InlineKeyboardButton(text="👥 Пользователи", callback_data="adm:users")],
        [InlineKeyboardButton(text="🛰 Пул и источники", callback_data="adm:sources"), InlineKeyboardButton(text="📣 Рассылка", callback_data="adm:broadcast")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="adm:home")],
    ])


def ticket_keyboard(ticket_id: str, status: str) -> InlineKeyboardMarkup:
    rows = []
    if status != "closed":
        rows.append([InlineKeyboardButton(text="✍️ Ответить", callback_data=f"adm:reply:{ticket_id}"),
                     InlineKeyboardButton(text="✅ Закрыть", callback_data=f"adm:close:{ticket_id}")])
    else:
        rows.append([InlineKeyboardButton(text="↩️ Переоткрыть", callback_data=f"adm:reopen:{ticket_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ К обращениям", callback_data="adm:tickets")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ticket_text(ticket: dict) -> str:
    sender = f"@{escape(ticket['username'])}" if ticket.get("username") else str(ticket.get("telegram_id") or "—")
    history = ticket.get("messages") or []
    lines = [f"📨 <b>Обращение</b> <code>{ticket['id'][:8]}</code>", f"От: {sender}", f"Статус: <b>{ticket['status']}</b>", ""]
    for item in history[-6:]:
        label = "Пользователь" if item["sender_type"] == "user" else "Администратор"
        lines.append(f"<b>{label}:</b> {escape(item['text'])}")
    return "\n".join(lines)


def broadcast_markup(draft: dict) -> InlineKeyboardMarkup | None:
    buttons = draft.get("buttons") or []
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=item["text"], url=item["url"])] for item in buttons
    ]) if buttons else None


async def ask_broadcast_segment(message: Message) -> None:
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Активные", callback_data="adm:segment:active"), InlineKeyboardButton(text="Все известные", callback_data="adm:segment:all")],
        [InlineKeyboardButton(text="С устройствами", callback_data="adm:segment:with_devices"), InlineKeyboardButton(text="Без устройств", callback_data="adm:segment:without_devices")],
        [InlineKeyboardButton(text="Отмена", callback_data="adm:broadcast")],
    ])
    await message.answer("Выберите сегмент получателей:", reply_markup=keyboard)


@dp.error()
async def record_telegram_block(event: ErrorEvent) -> bool:
    """Telegram does not provide a list of blockers; record a confirmed send failure."""
    if not isinstance(event.exception, TelegramForbiddenError):
        return False
    update = event.update
    origin = update.message or update.callback_query
    user = origin.from_user if origin else None
    if user:
        try:
            await api("POST", f"/internal/users/{user.id}/bot-blocked")
        except Exception:
            logging.exception("Unable to record Telegram bot block")
    return True


def menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Получить VPN", callback_data="vpn:get")],
        [InlineKeyboardButton(text="📘 Инструкция", callback_data="vpn:help"), InlineKeyboardButton(text="🛟 Поддержка", callback_data="vpn:support")],
        [InlineKeyboardButton(text="❤️ Поддержать проект", callback_data="donate:home")],
    ])


async def api(method: str, path: str, **kwargs):
    headers = {"X-Internal-Key": settings.internal_api_key}
    async with httpx.AsyncClient(base_url=settings.backend_internal_url, timeout=15) as client:
        response = await client.request(method, path, headers=headers, **kwargs)
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", "Сервис временно недоступен")
        except ValueError:
            detail = f"Сервис вернул HTTP {response.status_code}"
        raise RuntimeError(detail)
    return response.json()


async def ensure_user(message: Message | CallbackQuery) -> int:
    user = message.from_user
    assert user
    await api("POST", "/internal/users", json={"telegram_id": user.id, "username": user.username})
    return user.id


async def allowed(telegram_id: int, target_devices: int = 1) -> dict:
    return await api("POST", f"/internal/users/{telegram_id}/access", params={"target_devices": target_devices})


async def show_access_gate(callback: CallbackQuery, access: dict, target_devices: int) -> None:
    sponsors = access.get("sponsors") or []
    if sponsors:
        buttons = [[InlineKeyboardButton(text=f"➕ {item['button_text']}", url=item["link"])] for item in sponsors]
        buttons.append([InlineKeyboardButton(text="✅ Проверить подписки", callback_data=f"vpn:check:{target_devices}")])
        await callback.message.answer(
            f"🔒 <b>Нужны подписки для доступа</b>\n\n{access.get('reason') or 'Подпишитесь на партнёрские каналы'}.\n"
            "Откройте все каналы кнопками ниже, затем нажмите «Проверить подписки».",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
        return
    channels = await api("GET", "/internal/channels")
    lines = ["🔒 Для доступа подпишитесь на обязательные каналы:"]
    for channel in channels:
        link = f"https://t.me/{channel['username'].lstrip('@')}" if channel.get("username") else str(channel["chat_id"])
        lines.append(f"• {channel['title']} — {link}")
    await callback.message.answer("\n".join(lines) if channels else (access.get("reason") or "Доступ пока недоступен"), reply_markup=menu())


async def send_subscription(target: Message | CallbackQuery, device: dict) -> None:
    url = device["subscription_url"]
    token = url.rsplit("/", 1)[-1]
    landing_url = f"{settings.web_app_base_url.rstrip('/')}/connect?token={quote(token, safe='')}"
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=5, border=2)
    qr.add_data(landing_url)
    qr.make(fit=True)
    image = qr.make_image(fill_color="#27346B", back_color="#FFFFFF")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    caption = f"✨ <b>VPN готов</b>\n\n📱 Устройство {device['slot']}: {device['label']}\n\nОткройте страницу Zaza VPN: она скопирует личную подписку и покажет, как импортировать её в <b>HAPP</b>.\n\n📶 <b>Автоподключение Wi‑Fi</b> и 📡 <b>Автоподключение LTE</b> будут первыми серверами в HAPP."
    photo = BufferedInputFile(buffer.getvalue(), filename="vpn-subscription.png")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ Открыть Zaza VPN", url=landing_url)],
        [InlineKeyboardButton(text="📘 Инструкция", callback_data="vpn:help")],
    ])
    if isinstance(target, CallbackQuery):
        await target.message.answer_photo(photo, caption=caption, reply_markup=keyboard)
    else:
        await target.answer_photo(photo, caption=caption, reply_markup=keyboard)


async def send_star_invoice(message: Message, telegram_id: int, amount: int) -> None:
    intent = await api("POST", f"/internal/users/{telegram_id}/donations/stars", json={"amount": amount})
    await message.answer_invoice(
        title="Поддержка Zaza VPN",
        description="Добровольный донат на серверы и развитие проекта. Донат не открывает платные функции.",
        payload=intent["invoice_payload"],
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="Поддержать проект", amount=amount)],
        start_parameter=f"support-zaza-{intent['id'][:8]}",
    )


async def show_donation_home(target: Message | CallbackQuery) -> None:
    telegram_id = await ensure_user(target)
    await api("POST", f"/internal/users/{telegram_id}/events", json={"event_type": "donation_open"})
    summary = await api("GET", f"/internal/users/{telegram_id}/donations")
    rows = [
        [InlineKeyboardButton(text="⭐ 50", callback_data="donate:stars:50"), InlineKeyboardButton(text="⭐ 100", callback_data="donate:stars:100")],
        [InlineKeyboardButton(text="⭐ 250", callback_data="donate:stars:250"), InlineKeyboardButton(text="⭐ 500", callback_data="donate:stars:500")],
        [InlineKeyboardButton(text="✏️ Своя сумма Stars", callback_data="donate:stars:custom")],
    ]
    if summary.get("ton_enabled"):
        rows.append([InlineKeyboardButton(text="💎 Поддержать в TON", callback_data="donate:ton")])
    rows.append([InlineKeyboardButton(text="⬅️ В главное меню", callback_data="donate:back")])
    history = ""
    if summary.get("donations"):
        history = f"\n\nВаша поддержка: <b>{summary['stars']} ⭐</b> и <b>{summary['ton']} TON</b>. Спасибо!"
    text = (
        "❤️ <b>Поддержать Zaza VPN</b>\n\n"
        "Донаты идут на сервер, проверку VPN-конфигураций и развитие проекта. "
        "Это добровольная поддержка: бесплатный VPN останется бесплатным."
        f"{history}"
    )
    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    if isinstance(target, CallbackQuery):
        await target.message.answer(text, reply_markup=markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


@dp.message(CommandStart())
async def start(message: Message) -> None:
    telegram_id = await ensure_user(message)
    try:
        await api("POST", f"/internal/users/{telegram_id}/events", json={"event_type": "bot_start"})
    except Exception:
        logging.exception("Unable to record bot start analytics event")
    await message.answer("👋 <b>Добро пожаловать в Zaza VPN</b>\n\nБесплатный VPN с автоматическим выбором сильной ноды для Wi‑Fi и LTE.", reply_markup=menu())


@dp.message(Command("donate"))
async def donate_command(message: Message) -> None:
    await show_donation_home(message)


@dp.message(Command("terms"))
async def terms_command(message: Message) -> None:
    await message.answer(
        "📄 <b>Условия поддержки Zaza VPN</b>\n\n"
        "Донат является добровольной поддержкой проекта и не предоставляет платных функций, подписки или преимуществ. "
        "Для вопросов по платежам используйте /paysupport. Возврат Stars рассматривается владельцем проекта по обращению."
    )


@dp.message(Command("paysupport"))
async def payment_support_command(message: Message) -> None:
    telegram_id = await ensure_user(message)
    support_waiting.add(telegram_id)
    await message.answer("Опишите проблему с платежом одним сообщением. Укажите сумму и примерное время, но не присылайте данные кошелька или банковской карты.")


@dp.callback_query(F.data == "donate:home")
async def donation_home_callback(callback: CallbackQuery) -> None:
    await show_donation_home(callback)


@dp.callback_query(F.data == "donate:back")
async def donation_back_callback(callback: CallbackQuery) -> None:
    await callback.message.answer("Главное меню", reply_markup=menu())
    await callback.answer()


@dp.callback_query(F.data.startswith("donate:stars:"))
async def donation_stars_callback(callback: CallbackQuery) -> None:
    telegram_id = await ensure_user(callback)
    value = callback.data.rsplit(":", 1)[-1]
    if value == "custom":
        donation_custom_waiting.add(telegram_id)
        await callback.message.answer("Введите сумму от 1 до 10 000 Stars одним числом. Для отмены отправьте /cancel.")
        await callback.answer()
        return
    try:
        await send_star_invoice(callback.message, telegram_id, int(value))
        await callback.answer()
    except Exception as exc:
        await callback.answer(str(exc), show_alert=True)


@dp.callback_query(F.data == "donate:ton")
async def donation_ton_callback(callback: CallbackQuery) -> None:
    telegram_id = await ensure_user(callback)
    try:
        session = await api("POST", f"/internal/users/{telegram_id}/donations/ton")
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💎 Открыть TON-донат", url=session["url"])],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="donate:home")],
        ])
        await callback.message.answer("Подключите TON-кошелёк и подтвердите перевод. Zaza VPN никогда не запрашивает seed-фразу или приватный ключ.", reply_markup=keyboard)
        await callback.answer()
    except Exception as exc:
        await callback.answer(str(exc), show_alert=True)


@dp.pre_checkout_query()
async def donation_pre_checkout(query: PreCheckoutQuery) -> None:
    try:
        await api("POST", f"/internal/users/{query.from_user.id}/donations/stars/precheckout", json={
            "invoice_payload": query.invoice_payload,
            "currency": query.currency,
            "total_amount": query.total_amount,
        })
        await query.answer(ok=True)
    except Exception as exc:
        await query.answer(ok=False, error_message=str(exc)[:200])


@dp.message(F.successful_payment)
async def donation_success(message: Message) -> None:
    if not message.from_user or not message.successful_payment:
        return
    payment = message.successful_payment
    try:
        result = await api("POST", f"/internal/users/{message.from_user.id}/donations/stars/complete", json={
            "invoice_payload": payment.invoice_payload,
            "currency": payment.currency,
            "total_amount": payment.total_amount,
            "telegram_payment_charge_id": payment.telegram_payment_charge_id,
            "provider_payment_charge_id": payment.provider_payment_charge_id or None,
        })
        await message.answer(f"❤️ <b>Спасибо за поддержку!</b>\n\nПолучено <b>{result['amount']} ⭐</b>. Донат пойдёт на работу серверов и развитие Zaza VPN.", reply_markup=menu())
    except Exception:
        logging.exception("Unable to record successful Stars donation")
        await message.answer("Платёж прошёл в Telegram, но подтверждение временно не записалось. Напишите /paysupport — платёж не потеряется.")


async def show_admin_home(target: Message | CallbackQuery) -> None:
    user = target.from_user
    assert user
    info = await api("GET", f"/internal/admin/{user.id}/dashboard")
    ping = f"{round(info['average_ping'], 1)} мс" if info.get("average_ping") is not None else "—"
    text = (f"🛡 <b>Zaza VPN · админ-пульт</b>\n\n"
            f"Активные ноды: <b>{info['active_nodes']}</b>\n"
            f"Проблемные ноды: <b>{info['problem_nodes']}</b>\n"
            f"Средний ping: <b>{ping}</b>\n"
            f"Ошибки источников: <b>{info['source_errors']}</b>\n"
            f"Новые обращения: <b>{info['new_tickets']}</b>\n"
            f"Активные рассылки: <b>{info['active_broadcasts']}</b>")
    if isinstance(target, CallbackQuery):
        await target.message.answer(text, reply_markup=admin_menu())
        await target.answer()
    else:
        await target.answer(text, reply_markup=admin_menu())


@dp.message(Command("admin"))
async def admin_command(message: Message) -> None:
    try:
        await show_admin_home(message)
    except Exception as exc:
        await message.answer(f"⛔ {escape(str(exc))}")


@dp.callback_query(F.data == "adm:home")
async def admin_home_callback(callback: CallbackQuery) -> None:
    try:
        await show_admin_home(callback)
    except Exception as exc:
        await callback.answer(str(exc), show_alert=True)


@dp.callback_query(F.data == "adm:tickets")
async def admin_tickets_callback(callback: CallbackQuery) -> None:
    assert callback.from_user
    try:
        tickets = await api("GET", f"/internal/admin/{callback.from_user.id}/tickets", params={"limit": 10})
        if not tickets:
            await callback.message.answer("📭 Обращений пока нет.", reply_markup=admin_menu())
        for ticket in tickets:
            await callback.message.answer(ticket_text(ticket), reply_markup=ticket_keyboard(ticket["id"], ticket["status"]))
        await callback.answer()
    except Exception as exc:
        await callback.answer(str(exc), show_alert=True)


@dp.callback_query(F.data.startswith("adm:reply:"))
async def admin_ticket_reply(callback: CallbackQuery) -> None:
    assert callback.from_user and callback.data
    ticket_id = callback.data.rsplit(":", 1)[1]
    try:
        await api("POST", f"/internal/admin/{callback.from_user.id}/tickets/{ticket_id}/claim")
        admin_reply_waiting[callback.from_user.id] = ticket_id
        await callback.message.answer("✍️ Напишите ответ одним текстовым сообщением. /cancel — отмена.")
        await callback.answer("Обращение взято в работу")
    except Exception as exc:
        await callback.answer(str(exc), show_alert=True)


@dp.callback_query(F.data.startswith("adm:close:") | F.data.startswith("adm:reopen:"))
async def admin_ticket_status(callback: CallbackQuery) -> None:
    assert callback.from_user and callback.data
    _, action, ticket_id = callback.data.split(":")
    try:
        ticket = await api("POST", f"/internal/admin/{callback.from_user.id}/tickets/{ticket_id}/{action}")
        await callback.message.answer(ticket_text(ticket), reply_markup=ticket_keyboard(ticket["id"], ticket["status"]))
        await callback.answer("Статус обновлён")
    except Exception as exc:
        await callback.answer(str(exc), show_alert=True)


@dp.callback_query(F.data == "adm:users")
async def admin_users_callback(callback: CallbackQuery) -> None:
    assert callback.from_user
    try:
        await api("GET", f"/internal/admin/{callback.from_user.id}/dashboard")
        admin_user_search_waiting.add(callback.from_user.id)
        await callback.message.answer("🔎 Пришлите Telegram ID или @username пользователя. /cancel — отмена.")
        await callback.answer()
    except Exception as exc:
        await callback.answer(str(exc), show_alert=True)


@dp.callback_query(F.data.startswith("adm:blockconfirm:"))
async def admin_block_confirm(callback: CallbackQuery) -> None:
    assert callback.data
    user_id = callback.data.rsplit(":", 1)[1]
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подтвердить", callback_data=f"adm:block:{user_id}"),
         InlineKeyboardButton(text="Отмена", callback_data="adm:home")]
    ])
    await callback.message.answer("Изменить блокировку пользователя? Его текущие подписки перестанут или снова начнут выдаваться.", reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data.startswith("adm:block:"))
async def admin_block_user(callback: CallbackQuery) -> None:
    assert callback.from_user and callback.data
    user_id = callback.data.rsplit(":", 1)[1]
    try:
        result = await api("POST", f"/internal/admin/{callback.from_user.id}/users/{user_id}/block")
        await callback.answer("Пользователь заблокирован" if result["is_blocked"] else "Пользователь разблокирован", show_alert=True)
    except Exception as exc:
        await callback.answer(str(exc), show_alert=True)


@dp.callback_query(F.data == "adm:sources")
async def admin_sources_callback(callback: CallbackQuery) -> None:
    assert callback.from_user
    try:
        sources = await api("GET", f"/internal/admin/{callback.from_user.id}/sources")
        rows = [[InlineKeyboardButton(text=f"{'🔴' if item['last_error'] else '🟢'} {item['name']}", callback_data=f"adm:source:{item['id']}")] for item in sources[:12]]
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:home")])
        await callback.message.answer("🛰 <b>Источники</b>\nВыберите источник для ручного обновления:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await callback.answer()
    except Exception as exc:
        await callback.answer(str(exc), show_alert=True)


@dp.callback_query(F.data.startswith("adm:source:"))
async def admin_source_confirm(callback: CallbackQuery) -> None:
    assert callback.data
    source_id = callback.data.rsplit(":", 1)[1]
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Запустить обновление", callback_data=f"adm:refresh:{source_id}")],
        [InlineKeyboardButton(text="Отмена", callback_data="adm:sources")],
    ])
    await callback.message.answer("Обновить этот источник сейчас? Операция может занять несколько секунд.", reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data.startswith("adm:refresh:"))
async def admin_source_refresh(callback: CallbackQuery) -> None:
    assert callback.from_user and callback.data
    source_id = callback.data.rsplit(":", 1)[1]
    await callback.answer("Обновляю…")
    try:
        result = await api("POST", f"/internal/admin/{callback.from_user.id}/sources/{source_id}/refresh")
        await callback.message.answer(f"✅ <b>{escape(result['name'])}</b>\nСтатус: {result['status']}\nНайдено: {result['found_count']}\nОпубликовано: {result['published_count']}\n{escape(result.get('message') or '')}", reply_markup=admin_menu())
    except Exception as exc:
        await callback.message.answer(f"❌ {escape(str(exc))}", reply_markup=admin_menu())


@dp.callback_query(F.data == "adm:broadcast")
async def admin_broadcast_callback(callback: CallbackQuery) -> None:
    assert callback.from_user
    try:
        await broadcast_drafts.clear(callback.from_user.id)
        campaigns = await api("GET", f"/internal/admin/{callback.from_user.id}/broadcasts")
        lines = ["📣 <b>Рассылки</b>", "", "Последние кампании:"]
        for item in campaigns[:5]:
            lines.append(f"• <code>{item['id'][:8]}</code> · {item['status']} · {item['sent_count']}/{item['total_count']}")
        rows = [[InlineKeyboardButton(text="➕ Создать рассылку", callback_data="adm:broadcast:new")]]
        rows.extend([[InlineKeyboardButton(text=f"⛔ Отменить {item['id'][:8]}", callback_data=f"adm:bcancel:{item['id']}")]
                     for item in campaigns if item["status"] in {"queued", "processing"}])
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:home")])
        keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
        await callback.message.answer("\n".join(lines), reply_markup=keyboard)
        await callback.answer()
    except Exception as exc:
        await callback.answer(str(exc), show_alert=True)


@dp.callback_query(F.data == "adm:broadcast:new")
async def admin_broadcast_new(callback: CallbackQuery) -> None:
    assert callback.from_user
    try:
        await api("GET", f"/internal/admin/{callback.from_user.id}/dashboard")
        await broadcast_drafts.begin(callback.from_user.id)
        await callback.message.answer(
            "Пришлите текст или одну фотографию с подписью. Поддерживается Telegram HTML:\n"
            "<code>&lt;b&gt;жирный&lt;/b&gt;</code>, <code>&lt;i&gt;курсив&lt;/i&gt;</code>, "
            "<code>&lt;u&gt;подчёркнутый&lt;/u&gt;</code>, <code>&lt;s&gt;зачёркнутый&lt;/s&gt;</code>, "
            "<code>&lt;tg-spoiler&gt;спойлер&lt;/tg-spoiler&gt;</code>, <code>&lt;a href=\"https://site.ru\"&gt;ссылка&lt;/a&gt;</code>.\n"
            "/cancel — отмена."
        )
        await callback.answer()
    except Exception as exc:
        await callback.answer(str(exc), show_alert=True)


@dp.callback_query(F.data.startswith("adm:bcancel:"))
async def admin_broadcast_cancel(callback: CallbackQuery) -> None:
    assert callback.from_user and callback.data
    campaign_id = callback.data.rsplit(":", 1)[1]
    try:
        await api("POST", f"/internal/admin/{callback.from_user.id}/broadcasts/{campaign_id}/cancel")
        await callback.answer("Отмена запрошена", show_alert=True)
    except Exception as exc:
        await callback.answer(str(exc), show_alert=True)


@dp.callback_query(F.data.startswith("adm:segment:"))
async def admin_broadcast_segment(callback: CallbackQuery) -> None:
    assert callback.from_user and callback.data
    state = await broadcast_drafts.load(callback.from_user.id)
    if not state or state.get("stage") != "segment":
        await callback.answer("Черновик не найден, начните заново", show_alert=True)
        return
    draft = state["draft"]
    draft["segment"] = callback.data.rsplit(":", 1)[1]
    state["stage"] = "confirm"
    await broadcast_drafts.save(callback.from_user.id, state)
    markup = broadcast_markup(draft)
    if draft.get("photo_file_id"):
        await callback.message.answer_photo(draft["photo_file_id"], caption=draft.get("text_html") or None, reply_markup=markup)
    else:
        await callback.message.answer(draft["text_html"], disable_web_page_preview=True, reply_markup=markup)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧪 Отправить тест админам", callback_data="adm:broadcast:test")],
        [InlineKeyboardButton(text="🚀 Подтвердить отправку", callback_data="adm:broadcast:confirm")],
        [InlineKeyboardButton(text="Отмена", callback_data="adm:broadcast")],
    ])
    await callback.message.answer(f"Сегмент: <b>{draft['segment']}</b>\nПроверьте сообщение выше и подтвердите рассылку.", reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data == "adm:broadcast:test")
async def admin_broadcast_test(callback: CallbackQuery) -> None:
    assert callback.from_user
    state = await broadcast_drafts.load(callback.from_user.id)
    if not state or state.get("stage") != "confirm":
        await callback.answer("Черновик не найден", show_alert=True)
        return
    draft = state["draft"]
    try:
        recipients = await api("GET", f"/internal/admin/{callback.from_user.id}/broadcasts/test-recipients")
        markup = broadcast_markup(draft)
        delivered = 0
        for admin_id in recipients:
            try:
                if draft.get("photo_file_id"):
                    await callback.bot.send_photo(admin_id, draft["photo_file_id"], caption=draft.get("text_html") or None, reply_markup=markup)
                else:
                    await callback.bot.send_message(admin_id, draft["text_html"], disable_web_page_preview=True, reply_markup=markup)
                delivered += 1
            except Exception:
                logging.exception("Unable to deliver test broadcast to %s", admin_id)
        await callback.answer(f"Тест доставлен администраторам: {delivered}/{len(recipients)}", show_alert=True)
    except Exception as exc:
        await callback.answer(str(exc), show_alert=True)


@dp.callback_query(F.data == "adm:broadcast:confirm")
async def admin_broadcast_confirm(callback: CallbackQuery) -> None:
    assert callback.from_user
    state = await broadcast_drafts.load(callback.from_user.id)
    draft = state.get("draft") if state else None
    if not state or state.get("stage") != "confirm" or not draft or not draft.get("segment"):
        await callback.answer("Черновик не найден", show_alert=True)
        return
    try:
        payload = {**draft, "client_request_id": state["client_request_id"]}
        result = await api("POST", f"/internal/admin/{callback.from_user.id}/broadcasts", json=payload)
        await broadcast_drafts.clear(callback.from_user.id)
        await callback.message.answer(f"✅ Рассылка <code>{result['id'][:8]}</code> поставлена в очередь.", reply_markup=admin_menu())
        await callback.answer("Рассылка запущена")
    except Exception as exc:
        await callback.answer(str(exc), show_alert=True)


@dp.callback_query(F.data == "vpn:get")
async def access_flow(callback: CallbackQuery) -> None:
    telegram_id = await ensure_user(callback)
    devices = await api("GET", f"/internal/users/{telegram_id}/devices")
    if len(devices) >= 8:
        buttons = [[InlineKeyboardButton(text=f"♻️ Перевыпустить: {item['label']}", callback_data=f"vpn:rotate:{item['id']}")] for item in devices]
        await callback.message.answer("У вас уже 8 устройств. Выберите устройство для перевыпуска ссылки:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await callback.answer()
        return
    target_devices = len(devices) + 1
    access = await allowed(telegram_id, target_devices)
    if not access["allowed"]:
        await show_access_gate(callback, access, target_devices)
        await callback.answer()
        return
    device = await api("POST", f"/internal/users/{telegram_id}/devices", json={"label": f"Устройство {len(devices) + 1}"})
    await send_subscription(callback, device)
    await callback.answer()


@dp.callback_query(F.data.in_({"vpn:refresh", "vpn:check"}))
async def legacy_menu_buttons(callback: CallbackQuery) -> None:
    """Old messages remain in Telegram; make their removed buttons harmless."""
    await access_flow(callback)


@dp.callback_query(F.data.startswith("vpn:check:"))
async def check_partner_gate(callback: CallbackQuery) -> None:
    await access_flow(callback)


@dp.callback_query(F.data.startswith("vpn:rotate:"))
async def rotate(callback: CallbackQuery) -> None:
    telegram_id = await ensure_user(callback)
    access = await allowed(telegram_id)
    if not access["allowed"]:
        await callback.answer("Сначала выполните условия доступа", show_alert=True)
        return
    device_id = callback.data.rsplit(":", 1)[1]
    device = await api("POST", f"/internal/users/{telegram_id}/devices/{device_id}/rotate")
    await send_subscription(callback, device)
    await callback.answer("Ссылка перевыпущена")


@dp.callback_query(F.data == "vpn:help")
async def help_callback(callback: CallbackQuery) -> None:
    await callback.message.answer("1. Нажмите «Получить VPN».\n2. Откройте страницу Zaza VPN по QR-коду или кнопке.\n3. Нажмите «Скопировать ссылку» и установите HAPP.\n4. В HAPP выберите «+» → «Импорт по ссылке», вставьте ссылку и включите первый сервер.", reply_markup=menu())
    await callback.answer()


@dp.callback_query(F.data == "vpn:status")
async def status_callback(callback: CallbackQuery) -> None:
    info = await api("GET", "/internal/status")
    ping = f"{info['average_ping']} мс" if info.get("average_ping") is not None else "ещё измеряется"
    await callback.message.answer(f"Активных нод: {info['active_nodes']}\nСредний ping: {ping}", reply_markup=menu())
    await callback.answer()


@dp.callback_query(F.data == "vpn:support")
async def support_callback(callback: CallbackQuery) -> None:
    assert callback.from_user
    await ensure_user(callback)
    support_waiting.add(callback.from_user.id)
    await callback.message.answer("Опишите проблему одним сообщением: укажите клиент, устройство и что именно не работает. Я передам обращение администратору.", reply_markup=menu())
    await callback.answer()


@dp.message(F.text | F.photo)
async def state_message(message: Message) -> None:
    if not message.from_user:
        return
    telegram_id = message.from_user.id
    raw_text = message.text or message.caption or ""
    if raw_text == "/cancel":
        support_waiting.discard(telegram_id)
        donation_custom_waiting.discard(telegram_id)
        admin_reply_waiting.pop(telegram_id, None)
        admin_user_search_waiting.discard(telegram_id)
        await broadcast_drafts.clear(telegram_id)
        await message.answer("Действие отменено.", reply_markup=menu())
        return

    if telegram_id in donation_custom_waiting:
        if not message.text or not message.text.strip().isdigit():
            await message.answer("Введите целое число от 1 до 10 000 или отправьте /cancel.")
            return
        amount = int(message.text.strip())
        if not 1 <= amount <= 10000:
            await message.answer("Сумма должна быть от 1 до 10 000 Stars.")
            return
        donation_custom_waiting.discard(telegram_id)
        try:
            await send_star_invoice(message, telegram_id, amount)
        except Exception as exc:
            donation_custom_waiting.add(telegram_id)
            await message.answer(f"Не удалось создать счёт: {escape(str(exc))}")
        return

    if telegram_id in admin_reply_waiting:
        if not message.text:
            await message.answer("Ответ должен быть текстовым.")
            return
        ticket_id = admin_reply_waiting.pop(telegram_id)
        try:
            result = await api("POST", f"/internal/admin/{telegram_id}/tickets/{ticket_id}/reply", json={"text": message.text})
            await message.answer("✅ Ответ доставлен пользователю.", reply_markup=ticket_keyboard(ticket_id, result["ticket"]["status"]))
        except Exception as exc:
            admin_reply_waiting[telegram_id] = ticket_id
            await message.answer(f"❌ {escape(str(exc))}\nПопробуйте ещё раз или отправьте /cancel.")
        return

    if telegram_id in admin_user_search_waiting:
        if not message.text:
            await message.answer("Пришлите Telegram ID или @username текстом.")
            return
        admin_user_search_waiting.discard(telegram_id)
        try:
            user = await api("POST", f"/internal/admin/{telegram_id}/users/search", json={"query": message.text})
            name = f"@{escape(user['username'])}" if user.get("username") else "Без username"
            status = "заблокирован" if user["is_blocked"] else "активен"
            text = (f"👤 <b>{name}</b>\nID: <code>{user['telegram_id']}</code>\n"
                    f"Статус: <b>{status}</b>\nАктивных устройств: <b>{user['device_count']}</b>\n"
                    f"Проверка каналов: {escape(user.get('last_membership_check') or 'ещё не выполнялась')}")
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Разблокировать" if user["is_blocked"] else "Заблокировать", callback_data=f"adm:blockconfirm:{user['id']}")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:home")],
            ])
            await message.answer(text, reply_markup=keyboard)
        except Exception as exc:
            await message.answer(f"❌ {escape(str(exc))}", reply_markup=admin_menu())
        return

    broadcast_state = await broadcast_drafts.load(telegram_id)
    if broadcast_state and broadcast_state.get("stage") == "buttons":
        if not message.text:
            await message.answer("Пришлите кнопки текстом или отправьте /skip.")
            return
        buttons = []
        if message.text.strip() != "/skip":
            lines = [line.strip() for line in message.text.splitlines() if line.strip()]
            if len(lines) > 6:
                await message.answer("Можно добавить не более 6 кнопок.")
                return
            try:
                for line in lines:
                    label, url = [part.strip() for part in line.split("|", 1)]
                    parsed = urlparse(url)
                    if not label or len(label) > 64 or parsed.scheme not in {"http", "https", "tg"} or (parsed.scheme != "tg" and not parsed.netloc):
                        raise ValueError
                    buttons.append({"text": label, "url": url})
            except ValueError:
                await message.answer("Неверный формат. Каждая строка: <code>Текст кнопки | https://site.ru</code>")
                return
        broadcast_state["draft"]["buttons"] = buttons
        broadcast_state["stage"] = "segment"
        await broadcast_drafts.save(telegram_id, broadcast_state)
        await ask_broadcast_segment(message)
        return

    if broadcast_state and broadcast_state.get("stage") == "content":
        photo_file_id = message.photo[-1].file_id if message.photo else None
        source_text = raw_text
        if source_text and not ("<" in source_text and ">" in source_text):
            source_text = message.html_text if message.text else (message.html_caption or source_text)
        clean = sanitize_telegram_html(source_text)
        max_length = 1024 if photo_file_id else 4096
        if len(clean) > max_length or (not clean and not photo_file_id):
            await message.answer(f"Сообщение пустое или превышает лимит {max_length} символов.")
            return
        broadcast_state["draft"] = {"text_html": clean, "photo_file_id": photo_file_id, "buttons": []}
        broadcast_state["stage"] = "buttons"
        await broadcast_drafts.save(telegram_id, broadcast_state)
        await message.answer("Добавьте кнопки: каждая строка в формате\n<code>Текст кнопки | https://site.ru</code>\n\nДо 6 кнопок. Если кнопки не нужны — /skip.")
        return

    if telegram_id not in support_waiting:
        return
    if not message.text:
        await message.answer("Обращение должно быть текстовым.")
        return
    support_waiting.discard(telegram_id)
    try:
        ticket = await api("POST", f"/internal/users/{telegram_id}/tickets", json={"text": message.text})
        bot = message.bot
        if bot:
            for admin_id in ticket.pop("admin_ids", []):
                try:
                    await bot.send_message(admin_id, ticket_text(ticket), reply_markup=ticket_keyboard(ticket["id"], ticket["status"]))
                except Exception:
                    logging.exception("Unable to forward support ticket to %s", admin_id)
        await message.answer("✅ Обращение передано. Ответ придёт в этот чат.", reply_markup=menu())
    except Exception as exc:
        await message.answer(f"Не удалось передать обращение: {escape(str(exc))}", reply_markup=menu())


async def main() -> None:
    if not settings.telegram_bot_token:
        logging.warning("TELEGRAM_BOT_TOKEN is empty; bot will not start")
        return
    bot = Bot(settings.telegram_bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    try:
        await dp.start_polling(bot)
    finally:
        await broadcast_drafts.close()


if __name__ == "__main__":
    asyncio.run(main())

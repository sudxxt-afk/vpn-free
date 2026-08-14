import asyncio
import io
import logging
from html import escape
from urllib.parse import quote, urlparse

import httpx
import qrcode
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import (BufferedInputFile, CallbackQuery, ErrorEvent, InlineKeyboardButton, InlineKeyboardMarkup,
                           InputMediaPhoto, LabeledPrice, Message, PreCheckoutQuery)
from aiogram.utils.text_decorations import html_decoration

from app.config import get_settings
from app.services.broadcast_drafts import BroadcastDraftStore
from app.services.interaction_state import InteractionStateStore
from app.services.telegram_html import sanitize_telegram_html

logging.basicConfig(level=logging.INFO)
settings = get_settings()
dp = Dispatcher()
admin_reply_waiting: dict[int, str] = {}
admin_user_search_waiting: set[int] = set()
broadcast_drafts = BroadcastDraftStore()
interaction_states = InteractionStateStore()


async def clear_interactive_state(telegram_id: int, *, keep_broadcast: bool = False) -> None:
    """Do not let an abandoned flow capture input for a new one."""
    await interaction_states.clear(telegram_id)
    admin_reply_waiting.pop(telegram_id, None)
    admin_user_search_waiting.discard(telegram_id)
    if not keep_broadcast:
        await broadcast_drafts.clear(telegram_id)


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


def ticket_list_markup(tickets: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for ticket in tickets:
        status_icon = {"new": "🆕", "answered": "✉️", "closed": "✅"}.get(ticket.get("status"), "•")
        sender = f"@{ticket['username']}" if ticket.get("username") else str(ticket.get("telegram_id") or "пользователь")
        rows.append([InlineKeyboardButton(
            text=f"{status_icon} {sender} · {ticket['id'][:8]}",
            callback_data=f"adm:ticket:{ticket['id']}",
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def broadcast_markup(draft: dict) -> InlineKeyboardMarkup | None:
    buttons = draft.get("buttons") or []
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=item["text"], url=item["url"])] for item in buttons
    ]) if buttons else None


DOCUMENT_MEDIA_PREFIX = "document:"


def is_document_broadcast_media(file_id: str | None) -> bool:
    return bool(file_id and file_id.startswith(DOCUMENT_MEDIA_PREFIX))


def telegram_media_file_id(file_id: str) -> str:
    return file_id.removeprefix(DOCUMENT_MEDIA_PREFIX)


def telegram_message_html(message: Message, *, caption: bool = False) -> str:
    """Return Telegram's native formatting as HTML on every supported aiogram version."""
    text = getattr(message, "caption", None) if caption else getattr(message, "text", None)
    if not text:
        return ""
    # Admins may enter Telegram HTML directly; keep it for the sanitizer below.
    if "<" in text and ">" in text:
        return text
    entities = getattr(message, "caption_entities", None) if caption else getattr(message, "entities", None)
    return html_decoration.unparse(text, entities)


async def edit_callback_screen(
    callback: CallbackQuery,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    disable_web_page_preview: bool = True,
) -> None:
    """Replace an inline-navigation screen without losing actions on stale/media messages."""
    message = callback.message
    if not getattr(message, "text", None):
        await message.answer(text, reply_markup=reply_markup, disable_web_page_preview=disable_web_page_preview)
        return
    try:
        await message.edit_text(text, reply_markup=reply_markup, disable_web_page_preview=disable_web_page_preview)
    except TelegramBadRequest as exc:
        detail = str(exc).lower()
        if "message is not modified" in detail:
            return
        if any(fragment in detail for fragment in (
            "message can't be edited",
            "message to edit not found",
            "message_id_invalid",
            "message is too old",
        )):
            logging.info("Cannot edit navigation screen; sending a replacement: %s", exc)
            await message.answer(text, reply_markup=reply_markup, disable_web_page_preview=disable_web_page_preview)
            return
        raise


async def edit_callback_photo(
    callback: CallbackQuery,
    photo: BufferedInputFile,
    caption: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Turn a callback screen into a photo screen, with a safe stale-message fallback."""
    message = callback.message
    try:
        await message.edit_media(
            InputMediaPhoto(media=photo, caption=caption),
            reply_markup=reply_markup,
        )
    except TelegramBadRequest as exc:
        detail = str(exc).lower()
        if "message is not modified" in detail:
            return
        if any(fragment in detail for fragment in (
            "message can't be edited",
            "message to edit not found",
            "message_id_invalid",
            "message is too old",
        )):
            logging.info("Cannot edit QR screen; sending a replacement: %s", exc)
            await message.answer_photo(photo, caption=caption, reply_markup=reply_markup)
            return
        raise


async def edit_callback_qr_caption(
    callback: CallbackQuery,
    caption: str,
    *,
    reply_markup: InlineKeyboardMarkup,
) -> bool:
    """Edit QR instructions in place; keep old QR buttons from becoming dead ends."""
    try:
        await callback.message.edit_caption(caption, reply_markup=reply_markup)
        return True
    except TelegramBadRequest as exc:
        detail = str(exc).lower()
        if "message is not modified" in detail:
            return True
        if any(fragment in detail for fragment in (
            "message can't be edited",
            "message to edit not found",
            "message_id_invalid",
            "message is too old",
        )):
            logging.info("Cannot edit QR caption; using a replacement screen: %s", exc)
            await edit_callback_screen(callback, caption, reply_markup=menu())
            return False
        raise


def qr_keyboard(landing_url: str, *, show_back: bool = False) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="⚡ Открыть Zaza VPN", url=landing_url)]]
    if show_back:
        rows.append([InlineKeyboardButton(text="◀️ К QR-коду", callback_data="vpn:qr")])
    else:
        rows.append([InlineKeyboardButton(text="📘 Инструкция", callback_data="vpn:help")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def qr_landing_url(callback: CallbackQuery) -> str | None:
    """Recover the private landing link already attached to the current QR screen."""
    markup = getattr(callback.message, "reply_markup", None)
    for row in getattr(markup, "inline_keyboard", []) or []:
        for button in row:
            if button.url and button.url.startswith(f"{settings.web_app_base_url.rstrip('/')}/connect?"):
                return button.url
    return None


def broadcast_segment_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Активные", callback_data="adm:bc:segment:active"), InlineKeyboardButton(text="Все известные", callback_data="adm:bc:segment:all")],
        [InlineKeyboardButton(text="С устройствами", callback_data="adm:bc:segment:with_devices"), InlineKeyboardButton(text="Без устройств", callback_data="adm:bc:segment:without_devices")],
        [InlineKeyboardButton(text="Отмена", callback_data="adm:broadcast")],
    ])


def broadcast_buttons_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Добавить кнопки", callback_data="adm:bc:buttons")],
        [InlineKeyboardButton(text="➡️ Без кнопок", callback_data="adm:bc:no-buttons")],
        [InlineKeyboardButton(text="⬅️ Изменить сообщение", callback_data="adm:bc:edit:content")],
        [InlineKeyboardButton(text="Отмена", callback_data="adm:broadcast")],
    ])


def broadcast_preview_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧪 Отправить тест админам", callback_data="adm:bc:test")],
        [InlineKeyboardButton(text="🚀 Подтвердить отправку", callback_data="adm:bc:confirm")],
        [InlineKeyboardButton(text="✏️ Сообщение", callback_data="adm:bc:edit:content"), InlineKeyboardButton(text="👥 Сегмент", callback_data="adm:bc:edit:segment")],
        [InlineKeyboardButton(text="🔗 Кнопки", callback_data="adm:bc:edit:buttons")],
        [InlineKeyboardButton(text="Отмена", callback_data="adm:broadcast")],
    ])


async def show_broadcast_preview(target: Message | CallbackQuery, draft: dict) -> None:
    markup = broadcast_markup(draft)
    if isinstance(target, CallbackQuery):
        message = target.message
    else:
        message = target
    if draft.get("photo_file_id"):
        if is_document_broadcast_media(draft["photo_file_id"]):
            await message.answer_document(
                telegram_media_file_id(draft["photo_file_id"]),
                caption=draft.get("text_html") or None,
                reply_markup=markup,
            )
        else:
            await message.answer_photo(draft["photo_file_id"], caption=draft.get("text_html") or None, reply_markup=markup)
    else:
        await message.answer(draft["text_html"], disable_web_page_preview=True, reply_markup=markup)
    confirmation_text = f"Сегмент: <b>{draft['segment']}</b>\nПроверьте сообщение выше и подтвердите рассылку."
    if isinstance(target, CallbackQuery):
        await edit_callback_screen(target, confirmation_text, reply_markup=broadcast_preview_markup())
    else:
        await message.answer(confirmation_text, reply_markup=broadcast_preview_markup())


@dp.error()
async def record_telegram_block(event: ErrorEvent) -> bool:
    """Telegram does not provide a list of blockers; record a confirmed send failure."""
    if isinstance(event.exception, BotServiceUnavailable):
        origin = event.update.message or event.update.callback_query
        try:
            if isinstance(origin, CallbackQuery):
                await origin.answer(str(event.exception), show_alert=True)
            elif origin:
                await origin.answer(str(event.exception), reply_markup=menu())
        except Exception:
            logging.exception("Unable to show temporary API outage")
        return True
    if not isinstance(event.exception, TelegramForbiddenError):
        logging.error(
            "Unhandled Telegram update",
            exc_info=(type(event.exception), event.exception, event.exception.__traceback__),
        )
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
        [InlineKeyboardButton(text="📱 Мои устройства", callback_data="vpn:get")],
        [InlineKeyboardButton(text="📊 Проверить VPN", callback_data="vpn:status"), InlineKeyboardButton(text="📘 Инструкция", callback_data="vpn:help")],
        [InlineKeyboardButton(text="🛟 Поддержка", callback_data="vpn:support")],
        [InlineKeyboardButton(text="❤️ Поддержать проект", callback_data="donate:home")],
    ])


class BotServiceUnavailable(RuntimeError):
    pass


async def api(method: str, path: str, **kwargs):
    headers = {"X-Internal-Key": settings.internal_api_key}
    attempts = 2 if method.upper() == "GET" else 1
    response = None
    for attempt in range(attempts):
        try:
            async with httpx.AsyncClient(base_url=settings.backend_internal_url, timeout=httpx.Timeout(10, connect=3)) as client:
                response = await client.request(method, path, headers=headers, **kwargs)
            if response.status_code not in {502, 503, 504} or attempt + 1 == attempts:
                break
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            if attempt + 1 == attempts:
                raise BotServiceUnavailable("Сервис VPN перезапускается. Попробуйте ещё раз через 10 секунд.") from exc
        await asyncio.sleep(0.4)
    assert response is not None
    if response.status_code in {502, 503, 504}:
        raise BotServiceUnavailable("Сервис VPN перезапускается. Попробуйте ещё раз через 10 секунд.")
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


def sponsor_gate_screen(access: dict) -> tuple[str, InlineKeyboardMarkup]:
    sponsors = access.get("sponsors") or []
    buttons = [
        [InlineKeyboardButton(
            text=f"{index}. {item.get('button_text') or 'Подписаться'} · {item.get('title') or 'Канал партнёра'}"[:64],
            url=item["link"],
        )]
        for index, item in enumerate(sponsors, start=1)
    ]
    return (
        "🛡 <b>Zaza VPN остаётся полностью бесплатным</b>\n\n"
        "Мы не продаём доступ и не прячем нормальную скорость за тарифами. "
        "Чтобы оплачивать серверы, проверки нод и поддержку проекта, достаточно подписаться на несколько каналов партнёров.\n\n"
        "🥖 Эти подписки помогают админам держать VPN в рабочем состоянии — и иногда даже покупать хлеб.\n\n"
        "<b>Каналы спонсоров:</b>\n"
        "Открой каждую кнопку ниже и подпишись. После этого снова отправь /start — бот сразу проверит подписки и откроет VPN.",
        InlineKeyboardMarkup(inline_keyboard=buttons),
    )


async def show_access_gate(target: Message | CallbackQuery, access: dict) -> None:
    sponsors = access.get("sponsors") or []
    if sponsors:
        text, reply_markup = sponsor_gate_screen(access)
    else:
        text = access.get("reason") or "Доступ пока закрыт. Отправь /start ещё раз через несколько секунд."
        reply_markup = None
    if isinstance(target, CallbackQuery):
        await edit_callback_screen(target, text, reply_markup=reply_markup)
        await target.answer()
        return
    await target.answer(text, reply_markup=reply_markup)


async def require_vpn_access(target: Message | CallbackQuery) -> int | None:
    telegram_id = await ensure_user(target)
    access = await allowed(telegram_id)
    if access.get("allowed"):
        return telegram_id
    await show_access_gate(target, access)
    return None


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
    keyboard = qr_keyboard(landing_url)
    if isinstance(target, CallbackQuery):
        await edit_callback_photo(target, photo, caption, reply_markup=keyboard)
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
        await edit_callback_screen(target, text, reply_markup=markup)
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
    vpn_status = await api("GET", f"/internal/users/{telegram_id}/vpn-status")
    if vpn_status.get("can_restore"):
        restore_markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="♻️ Возобновить подписку", callback_data="vpn:restore")],
            *menu().inline_keyboard,
        ])
        await message.answer(
            "⚠️ <b>Старая подписка отключена</b>\n\nНажми «Возобновить подписку». "
            "Если есть задания спонсоров, бот сначала покажет их — новая VPN-ссылка появится только после подписки.",
            reply_markup=restore_markup,
        )
        return
    access = await allowed(telegram_id)
    if not access.get("allowed"):
        await show_access_gate(message, access)
        return
    await message.answer(
        "👋 <b>Добро пожаловать в Zaza VPN</b>\n\nБесплатный VPN с автоматическим выбором сильной ноды для Wi‑Fi и LTE.",
        reply_markup=menu(),
    )


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
    await interaction_states.set(telegram_id, "support", category="payment")
    await message.answer("Опишите проблему с платежом одним сообщением. Укажите сумму и примерное время, но не присылайте данные кошелька или банковской карты.")


@dp.callback_query(F.data == "donate:home")
async def donation_home_callback(callback: CallbackQuery) -> None:
    await show_donation_home(callback)


@dp.callback_query(F.data == "donate:back")
async def donation_back_callback(callback: CallbackQuery) -> None:
    await edit_callback_screen(callback, "Главное меню", reply_markup=menu())
    await callback.answer()


@dp.callback_query(F.data.startswith("donate:stars:"))
async def donation_stars_callback(callback: CallbackQuery) -> None:
    telegram_id = await ensure_user(callback)
    value = callback.data.rsplit(":", 1)[-1]
    if value == "custom":
        await interaction_states.set(telegram_id, "donation_custom")
        await edit_callback_screen(callback, "Введите сумму от 1 до 10 000 Stars одним числом. Для отмены отправьте /cancel.")
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
        await edit_callback_screen(
            callback,
            "Подключите TON-кошелёк и подтвердите перевод. Zaza VPN никогда не запрашивает seed-фразу или приватный ключ.",
            reply_markup=keyboard,
        )
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
        await edit_callback_screen(target, text, reply_markup=admin_menu())
        await target.answer()
    else:
        await target.answer(text, reply_markup=admin_menu())


@dp.message(Command("admin"))
async def admin_command(message: Message) -> None:
    try:
        if message.from_user:
            await clear_interactive_state(message.from_user.id)
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
            await edit_callback_screen(callback, "📭 Обращений пока нет.", reply_markup=admin_menu())
        else:
            await edit_callback_screen(
                callback,
                "📨 <b>Обращения</b>\nВыберите обращение, чтобы посмотреть историю и ответить.",
                reply_markup=ticket_list_markup(tickets),
            )
        await callback.answer()
    except Exception as exc:
        await callback.answer(str(exc), show_alert=True)


@dp.callback_query(F.data.startswith("adm:ticket:"))
async def admin_ticket_detail(callback: CallbackQuery) -> None:
    assert callback.from_user and callback.data
    ticket_id = callback.data.rsplit(":", 1)[1]
    try:
        tickets = await api("GET", f"/internal/admin/{callback.from_user.id}/tickets", params={"limit": 50})
        ticket = next((item for item in tickets if item["id"] == ticket_id), None)
        if not ticket:
            await callback.answer("Обращение уже недоступно", show_alert=True)
            return
        await edit_callback_screen(callback, ticket_text(ticket), reply_markup=ticket_keyboard(ticket["id"], ticket["status"]))
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
        await edit_callback_screen(
            callback,
            "✍️ Напишите ответ одним текстовым сообщением. /cancel — отмена.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ К обращению", callback_data=f"adm:ticket:{ticket_id}")],
            ]),
        )
        await callback.answer("Обращение взято в работу")
    except Exception as exc:
        await callback.answer(str(exc), show_alert=True)


@dp.callback_query(F.data.startswith("adm:close:") | F.data.startswith("adm:reopen:"))
async def admin_ticket_status(callback: CallbackQuery) -> None:
    assert callback.from_user and callback.data
    _, action, ticket_id = callback.data.split(":")
    try:
        ticket = await api("POST", f"/internal/admin/{callback.from_user.id}/tickets/{ticket_id}/{action}")
        await edit_callback_screen(callback, ticket_text(ticket), reply_markup=ticket_keyboard(ticket["id"], ticket["status"]))
        await callback.answer("Статус обновлён")
    except Exception as exc:
        await callback.answer(str(exc), show_alert=True)


@dp.callback_query(F.data == "adm:users")
async def admin_users_callback(callback: CallbackQuery) -> None:
    assert callback.from_user
    try:
        await api("GET", f"/internal/admin/{callback.from_user.id}/dashboard")
        admin_user_search_waiting.add(callback.from_user.id)
        await edit_callback_screen(callback, "🔎 Пришлите Telegram ID или @username пользователя. /cancel — отмена.")
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
    await edit_callback_screen(
        callback,
        "Изменить блокировку пользователя? Его текущие подписки перестанут или снова начнут выдаваться.",
        reply_markup=keyboard,
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("adm:block:"))
async def admin_block_user(callback: CallbackQuery) -> None:
    assert callback.from_user and callback.data
    user_id = callback.data.rsplit(":", 1)[1]
    try:
        result = await api("POST", f"/internal/admin/{callback.from_user.id}/users/{user_id}/block")
        await edit_callback_screen(
            callback,
            "✅ Пользователь заблокирован." if result["is_blocked"] else "✅ Пользователь разблокирован.",
            reply_markup=admin_menu(),
        )
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
        await edit_callback_screen(
            callback,
            "🛰 <b>Источники</b>\nВыберите источник для ручного обновления:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
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
    await edit_callback_screen(callback, "Обновить этот источник сейчас? Операция может занять несколько секунд.", reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data.startswith("adm:refresh:"))
async def admin_source_refresh(callback: CallbackQuery) -> None:
    assert callback.from_user and callback.data
    source_id = callback.data.rsplit(":", 1)[1]
    await callback.answer("Обновляю…")
    try:
        result = await api("POST", f"/internal/admin/{callback.from_user.id}/sources/{source_id}/refresh")
        await edit_callback_screen(
            callback,
            f"✅ <b>{escape(result['name'])}</b>\nСтатус: {result['status']}\nНайдено: {result['found_count']}\nОпубликовано: {result['published_count']}\n{escape(result.get('message') or '')}",
            reply_markup=admin_menu(),
        )
    except Exception as exc:
        await edit_callback_screen(callback, f"❌ {escape(str(exc))}", reply_markup=admin_menu())


async def show_broadcasts_screen(callback: CallbackQuery, *, clear_state: bool) -> None:
    assert callback.from_user
    if clear_state:
        await clear_interactive_state(callback.from_user.id)
    campaigns = await api("GET", f"/internal/admin/{callback.from_user.id}/broadcasts")
    lines = ["📣 <b>Рассылки</b>", "", "Последние кампании:"]
    for item in campaigns[:5]:
        lines.append(
            f"• <code>{item['id'][:8]}</code> · {item['status']} · "
            f"✅ {item['sent_count']}/{item['total_count']} · ❌ {item['failed_count']} · 🚫 {item['skipped_count']}"
        )
    rows = [[InlineKeyboardButton(text="➕ Создать рассылку", callback_data="adm:broadcast:new")]]
    rows.extend([[InlineKeyboardButton(text=f"⛔ Отменить {item['id'][:8]}", callback_data=f"adm:bcancel:{item['id']}")]
                 for item in campaigns if item["status"] in {"queued", "processing"}])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:home")])
    await edit_callback_screen(callback, "\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(F.data == "adm:broadcast")
async def admin_broadcast_callback(callback: CallbackQuery) -> None:
    try:
        await show_broadcasts_screen(callback, clear_state=True)
        await callback.answer()
    except Exception as exc:
        await callback.answer(str(exc), show_alert=True)


@dp.callback_query(F.data == "adm:broadcast:new")
async def admin_broadcast_new(callback: CallbackQuery) -> None:
    assert callback.from_user
    try:
        await api("GET", f"/internal/admin/{callback.from_user.id}/dashboard")
        await clear_interactive_state(callback.from_user.id)
        await broadcast_drafts.begin(callback.from_user.id)
        await edit_callback_screen(
            callback,
            "Пришлите сообщение для рассылки. Формат определяется автоматически:\n"
            "• текст — текстовая рассылка;\n"
            "• фото — рассылка с фото;\n"
            "• фото с подписью — фото и текст.\n\n"
            "Поддерживается Telegram HTML: <code>&lt;b&gt;жирный&lt;/b&gt;</code>, "
            "<code>&lt;i&gt;курсив&lt;/i&gt;</code>, <code>&lt;u&gt;подчёркнутый&lt;/u&gt;</code>, "
            "<code>&lt;s&gt;зачёркнутый&lt;/s&gt;</code>, <code>&lt;tg-spoiler&gt;спойлер&lt;/tg-spoiler&gt;</code>, "
            "<code>&lt;a href=\"https://site.ru\"&gt;ссылка&lt;/a&gt;</code>.\n\n"
            "Документы, видео и голосовые сообщения не поддерживаются. /cancel — отмена."
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
        await show_broadcasts_screen(callback, clear_state=False)
        await callback.answer("Отмена запрошена", show_alert=True)
    except Exception as exc:
        await callback.answer(str(exc), show_alert=True)


@dp.callback_query(F.data.startswith("adm:bc:segment:"))
async def admin_broadcast_segment(callback: CallbackQuery) -> None:
    assert callback.from_user and callback.data
    state = await broadcast_drafts.load(callback.from_user.id)
    if not state or state.get("stage") != "segment":
        await callback.answer("Черновик не найден, начните заново", show_alert=True)
        return
    state["draft"]["segment"] = callback.data.rsplit(":", 1)[1]
    state["stage"] = "buttons"
    await broadcast_drafts.save(callback.from_user.id, state)
    await edit_callback_screen(
        callback,
        "Добавить кнопки со ссылками? Их можно пропустить.",
        reply_markup=broadcast_buttons_markup(),
    )
    await callback.answer()


@dp.callback_query(F.data == "adm:bc:buttons")
async def admin_broadcast_add_buttons(callback: CallbackQuery) -> None:
    assert callback.from_user
    state = await broadcast_drafts.load(callback.from_user.id)
    if not state or state.get("stage") != "buttons":
        await callback.answer("Черновик не найден", show_alert=True)
        return
    await edit_callback_screen(
        callback,
        "Пришлите до 6 кнопок: каждая строка в формате\n"
        "<code>Текст кнопки | https://site.ru</code>\n\n"
        "Можно отправить /skip, чтобы продолжить без кнопок."
    )
    await callback.answer()


@dp.callback_query(F.data == "adm:bc:no-buttons")
async def admin_broadcast_skip_buttons(callback: CallbackQuery) -> None:
    assert callback.from_user
    state = await broadcast_drafts.load(callback.from_user.id)
    if not state or state.get("stage") != "buttons":
        await callback.answer("Черновик не найден", show_alert=True)
        return
    state["draft"]["buttons"] = []
    state["stage"] = "preview"
    await broadcast_drafts.save(callback.from_user.id, state)
    await show_broadcast_preview(callback, state["draft"])
    await callback.answer()


@dp.callback_query(F.data.startswith("adm:bc:edit:"))
async def admin_broadcast_edit(callback: CallbackQuery) -> None:
    assert callback.from_user and callback.data
    state = await broadcast_drafts.load(callback.from_user.id)
    if not state:
        await callback.answer("Черновик не найден", show_alert=True)
        return
    action = callback.data.rsplit(":", 1)[1]
    if action == "content" and state.get("stage") in {"buttons", "preview"}:
        state["draft"].update({"kind": None, "text_html": "", "photo_file_id": None, "buttons": []})
        state["stage"] = "content"
        await broadcast_drafts.save(callback.from_user.id, state)
        await edit_callback_screen(callback, "Пришлите новое сообщение: текст, фото или фото с подписью.")
    elif action == "segment" and state.get("stage") == "preview":
        state["stage"] = "segment"
        await broadcast_drafts.save(callback.from_user.id, state)
        await edit_callback_screen(callback, "Выберите новый сегмент получателей:", reply_markup=broadcast_segment_markup())
    elif action == "buttons" and state.get("stage") == "preview":
        state["stage"] = "buttons"
        await broadcast_drafts.save(callback.from_user.id, state)
        await edit_callback_screen(callback, "Измените кнопки или пропустите их:", reply_markup=broadcast_buttons_markup())
    else:
        await callback.answer("Этот шаг уже недоступен", show_alert=True)
        return
    await callback.answer()


@dp.callback_query(F.data == "adm:bc:test")
async def admin_broadcast_test(callback: CallbackQuery) -> None:
    assert callback.from_user
    state = await broadcast_drafts.load(callback.from_user.id)
    if not state or state.get("stage") != "preview":
        await callback.answer("Сначала соберите рассылку и откройте предпросмотр", show_alert=True)
        return
    draft = state["draft"]
    try:
        recipients = await api("GET", f"/internal/admin/{callback.from_user.id}/broadcasts/test-recipients")
        markup = broadcast_markup(draft)
        delivered = 0
        for admin_id in recipients:
            try:
                if draft.get("photo_file_id"):
                    if is_document_broadcast_media(draft["photo_file_id"]):
                        await callback.bot.send_document(
                            admin_id,
                            telegram_media_file_id(draft["photo_file_id"]),
                            caption=draft.get("text_html") or None,
                            reply_markup=markup,
                        )
                    else:
                        await callback.bot.send_photo(admin_id, draft["photo_file_id"], caption=draft.get("text_html") or None, reply_markup=markup)
                else:
                    await callback.bot.send_message(admin_id, draft["text_html"], disable_web_page_preview=True, reply_markup=markup)
                delivered += 1
            except Exception:
                logging.exception("Unable to deliver test broadcast to %s", admin_id)
        await callback.answer(f"Тест доставлен администраторам: {delivered}/{len(recipients)}", show_alert=True)
    except Exception as exc:
        await callback.answer(str(exc), show_alert=True)


@dp.callback_query(F.data == "adm:bc:confirm")
async def admin_broadcast_confirm(callback: CallbackQuery) -> None:
    assert callback.from_user
    state = await broadcast_drafts.load(callback.from_user.id)
    draft = state.get("draft") if state else None
    if not state or state.get("stage") != "preview" or not draft or not draft.get("segment"):
        await callback.answer("Сначала соберите рассылку и откройте предпросмотр", show_alert=True)
        return
    try:
        payload = {
            "client_request_id": state["client_request_id"],
            "segment": draft["segment"],
            "text_html": draft.get("text_html") or "",
            "photo_file_id": draft.get("photo_file_id"),
            "buttons": draft.get("buttons") or [],
        }
        result = await api("POST", f"/internal/admin/{callback.from_user.id}/broadcasts", json=payload)
        await broadcast_drafts.clear(callback.from_user.id)
        await edit_callback_screen(
            callback,
            f"✅ Рассылка <code>{result['id'][:8]}</code> поставлена в очередь. "
            "После завершения я пришлю полный отчёт по доставке.",
            reply_markup=admin_menu(),
        )
        await callback.answer("Рассылка запущена")
    except Exception as exc:
        await callback.answer(str(exc), show_alert=True)


async def show_devices(callback: CallbackQuery) -> None:
    telegram_id = await ensure_user(callback)
    devices = await api("GET", f"/internal/users/{telegram_id}/devices")
    status = await api("GET", f"/internal/users/{telegram_id}/vpn-status")
    rows = [[InlineKeyboardButton(
        text=f"{item['slot']}. {item['label']} · {'использовалось' if item.get('last_used_at') else 'не подключено'}",
        callback_data=f"vpn:device:{item['id']}",
    )] for item in devices]
    if len(devices) < 8:
        rows.append([InlineKeyboardButton(text="➕ Добавить устройство", callback_data="vpn:add")])
    if status.get("can_restore"):
        rows.append([InlineKeyboardButton(text="♻️ Возобновить подписку", callback_data="vpn:restore")])
    rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data="vpn:home")])
    last_open = status.get("last_subscription_open")
    await edit_callback_screen(
        callback,
        f"📱 <b>Мои устройства: {len(devices)}/8</b>\n\n"
        f"Последнее обновление в HAPP: <b>{last_open[:16].replace('T', ' ') if last_open else 'ещё не было'}</b>.\n"
        "Открой устройство, чтобы переименовать, удалить или безопасно перевыпустить ссылку.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@dp.callback_query(F.data == "vpn:get")
async def access_flow(callback: CallbackQuery) -> None:
    if await require_vpn_access(callback) is None:
        return
    await show_devices(callback)


@dp.callback_query(F.data == "vpn:home")
async def vpn_home(callback: CallbackQuery) -> None:
    await edit_callback_screen(callback, "Главное меню", reply_markup=menu())
    await callback.answer()


@dp.callback_query(F.data == "vpn:add")
async def add_device(callback: CallbackQuery) -> None:
    telegram_id = await require_vpn_access(callback)
    if telegram_id is None:
        return
    devices = await api("GET", f"/internal/users/{telegram_id}/devices")
    if len(devices) >= 8:
        await callback.answer("Достигнут лимит 8 устройств", show_alert=True)
        return
    label = f"Устройство {len(devices) + 1}"
    device = await api("POST", f"/internal/users/{telegram_id}/devices", json={"label": label})
    await send_subscription(callback, device)
    await callback.answer()


@dp.callback_query(F.data == "vpn:restore")
async def restore_subscription(callback: CallbackQuery) -> None:
    telegram_id = await require_vpn_access(callback)
    if telegram_id is None:
        return
    try:
        device = await api("POST", f"/internal/users/{telegram_id}/subscription/restore")
        await send_subscription(callback, device)
        await callback.answer("Подписка возобновлена")
    except RuntimeError as exc:
        await callback.answer(str(exc), show_alert=True)


@dp.callback_query(F.data.startswith("vpn:device:"))
async def device_details(callback: CallbackQuery) -> None:
    telegram_id = await ensure_user(callback)
    device_id = callback.data.rsplit(":", 1)[1]
    devices = await api("GET", f"/internal/users/{telegram_id}/devices")
    device = next((item for item in devices if item["id"] == device_id), None)
    if not device:
        await callback.answer("Устройство уже удалено", show_alert=True)
        await show_devices(callback)
        return
    last_used = device.get("last_used_at")
    rows = [
        [InlineKeyboardButton(text="✏️ Переименовать", callback_data=f"vpn:rename:{device_id}"),
         InlineKeyboardButton(text="♻️ Перевыпустить", callback_data=f"vpn:rotate:{device_id}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"vpn:delete-confirm:{device_id}")],
        [InlineKeyboardButton(text="⬅️ К устройствам", callback_data="vpn:get")],
    ]
    await edit_callback_screen(
        callback,
        f"📱 <b>{escape(device['label'])}</b>\n\nСлот: {device['slot']} из 8\n"
        f"Создано: {(device.get('created_at') or '')[:10] or '—'}\n"
        f"Последнее обновление HAPP: {(last_used or '')[:16].replace('T', ' ') or 'ещё не было'}\n"
        f"Ключ: …{device['token_hint']}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("vpn:rename:"))
async def rename_device_prompt(callback: CallbackQuery) -> None:
    assert callback.from_user
    device_id = callback.data.rsplit(":", 1)[1]
    await interaction_states.set(callback.from_user.id, "rename_device", device_id=device_id)
    await edit_callback_screen(callback, "Отправь новое название устройства одним сообщением (до 80 символов). Для отмены: /cancel")
    await callback.answer()


@dp.callback_query(F.data.startswith("vpn:delete-confirm:"))
async def delete_device_confirm(callback: CallbackQuery) -> None:
    device_id = callback.data.rsplit(":", 1)[1]
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Удалить устройство", callback_data=f"vpn:delete:{device_id}")],
        [InlineKeyboardButton(text="Отмена", callback_data=f"vpn:device:{device_id}")],
    ])
    await edit_callback_screen(callback, "Удалить устройство? Его текущая ссылка перестанет работать, восстановить её будет нельзя.", reply_markup=markup)
    await callback.answer()


@dp.callback_query(F.data.startswith("vpn:delete:"))
async def delete_device(callback: CallbackQuery) -> None:
    telegram_id = await ensure_user(callback)
    device_id = callback.data.rsplit(":", 1)[1]
    await api("DELETE", f"/internal/users/{telegram_id}/devices/{device_id}")
    await callback.answer("Устройство удалено")
    await show_devices(callback)


@dp.callback_query(F.data.in_({"vpn:refresh", "vpn:check"}))
async def legacy_menu_buttons(callback: CallbackQuery) -> None:
    """Old messages remain in Telegram; make their removed buttons harmless."""
    await access_flow(callback)


@dp.callback_query(F.data.startswith("vpn:rotate:"))
async def rotate(callback: CallbackQuery) -> None:
    telegram_id = await require_vpn_access(callback)
    if telegram_id is None:
        return
    device_id = callback.data.rsplit(":", 1)[1]
    device = await api("POST", f"/internal/users/{telegram_id}/devices/{device_id}/rotate")
    await send_subscription(callback, device)
    await callback.answer("Ссылка перевыпущена")


@dp.callback_query(F.data == "vpn:help")
async def help_callback(callback: CallbackQuery) -> None:
    landing_url = qr_landing_url(callback)
    if not getattr(callback.message, "photo", None) or not landing_url:
        # An old button can be attached to a non-QR message. Preserve a usable path
        # instead of trying to edit a caption where Telegram cannot display one.
        await edit_callback_screen(
            callback,
            "1. Нажмите «Получить VPN».\n2. Откройте страницу Zaza VPN по QR-коду или кнопке.\n3. Нажмите «Скопировать ссылку» и установите HAPP.\n4. В HAPP выберите «+» → «Импорт по ссылке», вставьте ссылку и включите первый сервер.",
            reply_markup=menu(),
        )
        await callback.answer()
        return
    await edit_callback_qr_caption(
        callback,
        "📘 <b>Как подключиться</b>\n\n"
        "1. Откройте страницу Zaza VPN кнопкой или отсканируйте QR-код.\n"
        "2. Нажмите «Скопировать ссылку» и установите HAPP.\n"
        "3. В HAPP выберите «+» → «Импорт по ссылке», вставьте ссылку.\n"
        "4. Включите первый сервер — для Wi‑Fi или LTE.",
        reply_markup=qr_keyboard(landing_url, show_back=True),
    )
    await callback.answer()


@dp.callback_query(F.data == "vpn:qr")
async def qr_callback(callback: CallbackQuery) -> None:
    landing_url = qr_landing_url(callback)
    if not getattr(callback.message, "photo", None) or not landing_url:
        await edit_callback_screen(callback, "Главное меню", reply_markup=menu())
        await callback.answer()
        return
    await edit_callback_qr_caption(
        callback,
        "✨ <b>VPN готов</b>\n\n"
        "Откройте страницу Zaza VPN кнопкой или отсканируйте QR-код. Там будет личная ссылка и инструкция для HAPP.",
        reply_markup=qr_keyboard(landing_url),
    )
    await callback.answer()


@dp.callback_query(F.data == "vpn:status")
async def status_callback(callback: CallbackQuery) -> None:
    telegram_id = await ensure_user(callback)
    info = await api("GET", f"/internal/users/{telegram_id}/vpn-status")
    ping = f"{info['average_ping']} мс" if info.get("average_ping") is not None else "ещё измеряется"
    last_open = info.get("last_subscription_open")
    await edit_callback_screen(
        callback,
        "📊 <b>Диагностика Zaza VPN</b>\n\n"
        f"Сеть: <b>{info['active_nodes']} активных серверов</b>\n"
        f"Средний ping: <b>{ping}</b>\n"
        f"Устройства: <b>{info['active_devices']}/{info['device_limit']}</b>\n"
        f"Последнее обновление HAPP: <b>{last_open[:16].replace('T', ' ') if last_open else 'не зафиксировано'}</b>\n"
        f"Доступ: <b>{info['access_status']}</b>\n\n"
        "Если HAPP не подключается, сначала обнови подписку в приложении. Если дата выше не изменится, открой поддержку.",
        reply_markup=menu(),
    )
    await callback.answer()


@dp.callback_query(F.data == "vpn:support")
async def support_callback(callback: CallbackQuery) -> None:
    assert callback.from_user
    await ensure_user(callback)
    categories = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔌 Не подключается", callback_data="support:category:connection"),
         InlineKeyboardButton(text="📱 Ошибка HAPP", callback_data="support:category:happ")],
        [InlineKeyboardButton(text="🐢 Низкая скорость", callback_data="support:category:speed"),
         InlineKeyboardButton(text="💳 Платёж", callback_data="support:category:payment")],
        [InlineKeyboardButton(text="Другое", callback_data="support:category:other")],
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="vpn:home")],
    ])
    await edit_callback_screen(
        callback,
        "🛟 <b>Поддержка</b>\n\nВыбери тип проблемы. После этого можно отправить текст или скриншот с подписью.",
        reply_markup=categories,
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("support:category:"))
async def support_category(callback: CallbackQuery) -> None:
    assert callback.from_user
    category = callback.data.rsplit(":", 1)[1]
    await interaction_states.set(callback.from_user.id, "support", category=category)
    await edit_callback_screen(
        callback,
        "Опиши проблему одним сообщением. Можно приложить скриншот и подпись. Бот автоматически добавит диагностику VPN и данные устройства.\n\nДля отмены: /cancel",
    )
    await callback.answer()


@dp.message()
async def state_message(message: Message) -> None:
    if not message.from_user:
        return
    telegram_id = message.from_user.id
    raw_text = message.text or message.caption or ""
    if raw_text == "/cancel":
        await clear_interactive_state(telegram_id)
        await message.answer("Действие отменено.", reply_markup=menu())
        return

    interaction = await interaction_states.get(telegram_id)
    if interaction and interaction.get("kind") == "donation_custom":
        if not message.text or not message.text.strip().isdigit():
            await message.answer("Введите целое число от 1 до 10 000 или отправьте /cancel.")
            return
        amount = int(message.text.strip())
        if not 1 <= amount <= 10000:
            await message.answer("Сумма должна быть от 1 до 10 000 Stars.")
            return
        await interaction_states.clear(telegram_id)
        try:
            await send_star_invoice(message, telegram_id, amount)
        except Exception as exc:
            await interaction_states.set(telegram_id, "donation_custom")
            await message.answer(f"Не удалось создать счёт: {escape(str(exc))}")
        return

    if interaction and interaction.get("kind") == "rename_device":
        label = (message.text or "").strip()
        if not label or len(label) > 80:
            await message.answer("Название должно содержать от 1 до 80 символов или отправь /cancel.")
            return
        await api("PATCH", f"/internal/users/{telegram_id}/devices/{interaction['device_id']}", json={"label": label})
        await interaction_states.clear(telegram_id)
        await message.answer(f"✅ Устройство переименовано: <b>{escape(label)}</b>", reply_markup=menu())
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
    if broadcast_state and broadcast_state.get("stage") == "content":
        if message.photo:
            photo_file_id = message.photo[-1].file_id
            source_text = telegram_message_html(message, caption=True)
            clean = sanitize_telegram_html(source_text)
            if len(clean) > 1024:
                await message.answer("Подпись к фото превышает лимит Telegram: 1024 символа.")
                return
            kind = "photo_caption" if clean else "photo"
        elif message.document:
            document = message.document
            image_extensions = (".jpg", ".jpeg", ".png", ".webp", ".gif")
            filename = (document.file_name or "").lower()
            is_image = (document.mime_type or "").startswith("image/") or filename.endswith(image_extensions)
            if not is_image:
                await message.answer("Это не картинка. Пришлите текст, фото или файл с изображением.")
                return
            photo_file_id = f"{DOCUMENT_MEDIA_PREFIX}{document.file_id}"
            source_text = telegram_message_html(message, caption=True)
            clean = sanitize_telegram_html(source_text)
            if len(clean) > 1024:
                await message.answer("Подпись к картинке превышает лимит Telegram: 1024 символа.")
                return
            kind = "document_caption" if clean else "document"
        elif message.text:
            photo_file_id = None
            source_text = telegram_message_html(message)
            clean = sanitize_telegram_html(source_text)
            if not clean or len(clean) > 4096:
                await message.answer("Текст пустой или превышает лимит Telegram: 4096 символов.")
                return
            kind = "text"
        else:
            await message.answer("Пришлите текст, фото или фото с подписью. Документы, видео и голосовые сообщения не поддерживаются.")
            return
        broadcast_state["draft"].update({
            "kind": kind,
            "text_html": clean,
            "photo_file_id": photo_file_id,
            "buttons": [],
        })
        broadcast_state["stage"] = "segment"
        await broadcast_drafts.save(telegram_id, broadcast_state)
        content_label = {
            "text": "текст",
            "photo": "фото",
            "photo_caption": "фото с подписью",
            "document": "картинка",
            "document_caption": "картинка с подписью",
        }[kind]
        await message.answer(
            f"✅ Получено: <b>{content_label}</b>. Теперь выберите сегмент получателей:",
            reply_markup=broadcast_segment_markup(),
        )
        return

    if broadcast_state and broadcast_state.get("stage") == "buttons":
        if not message.text:
            await message.answer("Пришлите кнопки текстом или нажмите «Без кнопок».")
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
        broadcast_state["stage"] = "preview"
        await broadcast_drafts.save(telegram_id, broadcast_state)
        await show_broadcast_preview(message, broadcast_state["draft"])
        return

    if not interaction or interaction.get("kind") != "support":
        return
    text = (message.text or message.caption or "").strip()
    if not text:
        await message.answer("Добавь описание проблемы к скриншоту или отправь текстом.")
        return
    try:
        diagnostics = await api("GET", f"/internal/users/{telegram_id}/vpn-status")
        photo_file_id = message.photo[-1].file_id if message.photo else None
        metadata = (
            f"\n\n---\nДиагностика: устройства {diagnostics['active_devices']}/{diagnostics['device_limit']}, "
            f"ноды {diagnostics['active_nodes']}, ping {diagnostics.get('average_ping') or '—'} мс, "
            f"доступ {diagnostics['access_status']}, язык {message.from_user.language_code or '—'}"
        )
        ticket = await api("POST", f"/internal/users/{telegram_id}/tickets", json={
            "text": text + metadata,
            "category": interaction.get("category", "other"),
            "telegram_file_id": photo_file_id,
        })
        await interaction_states.clear(telegram_id)
        bot = message.bot
        if bot:
            for admin_id in ticket.pop("admin_ids", []):
                try:
                    if photo_file_id:
                        await bot.send_photo(admin_id, photo_file_id, caption=f"Скриншот к обращению <code>{ticket['id'][:8]}</code>")
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
        await interaction_states.close()


if __name__ == "__main__":
    asyncio.run(main())

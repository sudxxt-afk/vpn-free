import asyncio
import io
import logging
from urllib.parse import quote

import httpx
import qrcode
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import get_settings

logging.basicConfig(level=logging.INFO)
settings = get_settings()
dp = Dispatcher()
support_waiting: set[int] = set()


def menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Получить VPN", callback_data="vpn:get")],
        [InlineKeyboardButton(text="📘 Инструкция", callback_data="vpn:help"), InlineKeyboardButton(text="🛟 Поддержка", callback_data="vpn:support")],
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


async def allowed(telegram_id: int) -> tuple[bool, str | None]:
    result = await api("POST", f"/internal/users/{telegram_id}/access")
    return result["allowed"], result.get("reason")


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


@dp.message(CommandStart())
async def start(message: Message) -> None:
    await ensure_user(message)
    await message.answer("👋 <b>Добро пожаловать в Zaza VPN</b>\n\nБесплатный VPN с автоматическим выбором сильной ноды для Wi‑Fi и LTE.", reply_markup=menu())


@dp.callback_query(F.data == "vpn:get")
async def access_flow(callback: CallbackQuery) -> None:
    telegram_id = await ensure_user(callback)
    is_allowed, reason = await allowed(telegram_id)
    if not is_allowed:
        channels = await api("GET", "/internal/channels")
        lines = ["Для доступа подпишитесь на все обязательные каналы:"]
        for channel in channels:
            link = f"https://t.me/{channel['username'].lstrip('@')}" if channel.get("username") else str(channel["chat_id"])
            lines.append(f"• {channel['title']} — {link}")
        lines.append("\nПосле подписки нажмите «Проверить каналы».")
        await callback.message.answer("\n".join(lines) if channels else (reason or "Доступ пока недоступен"), reply_markup=menu())
        await callback.answer()
        return
    devices = await api("GET", f"/internal/users/{telegram_id}/devices")
    if len(devices) >= 2:
        buttons = [[InlineKeyboardButton(text=f"♻️ Перевыпустить: {item['label']}", callback_data=f"vpn:rotate:{item['id']}")] for item in devices]
        await callback.message.answer("У вас уже две ячейки устройств. Выберите, для какой перевыпустить ссылку:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await callback.answer()
        return
    device = await api("POST", f"/internal/users/{telegram_id}/devices", json={"label": f"Устройство {len(devices) + 1}"})
    await send_subscription(callback, device)
    await callback.answer()


@dp.callback_query(F.data.in_({"vpn:refresh", "vpn:check"}))
async def legacy_menu_buttons(callback: CallbackQuery) -> None:
    """Old messages remain in Telegram; make their removed buttons harmless."""
    await access_flow(callback)


@dp.callback_query(F.data.startswith("vpn:rotate:"))
async def rotate(callback: CallbackQuery) -> None:
    telegram_id = await ensure_user(callback)
    is_allowed, _ = await allowed(telegram_id)
    if not is_allowed:
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
    support_waiting.add(callback.from_user.id)
    await callback.message.answer("Опишите проблему одним сообщением: укажите клиент, устройство и что именно не работает. Я передам обращение администратору.", reply_markup=menu())
    await callback.answer()


@dp.message(F.text)
async def support_message(message: Message) -> None:
    """Forward the next text message after the support action to configured admins."""
    if not message.from_user or message.from_user.id not in support_waiting:
        return
    support_waiting.discard(message.from_user.id)
    if not settings.admin_ids:
        await message.answer("Поддержка пока не настроена. Попробуйте обновить подписку или обратитесь к владельцу бота.", reply_markup=menu())
        return
    sender = f"@{message.from_user.username}" if message.from_user.username else str(message.from_user.id)
    text = f"📨 Поддержка VECTOR\nОт: {sender}\nID: {message.from_user.id}\n\n{message.text}"
    bot = message.bot
    if not bot:
        await message.answer("Не удалось передать обращение. Попробуйте ещё раз позже.", reply_markup=menu())
        return
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            logging.exception("Unable to forward support ticket to %s", admin_id)
    await message.answer("Обращение передано. Мы ответим в этом чате, когда появится решение.", reply_markup=menu())


async def main() -> None:
    if not settings.telegram_bot_token:
        logging.warning("TELEGRAM_BOT_TOKEN is empty; bot will not start")
        return
    bot = Bot(settings.telegram_bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

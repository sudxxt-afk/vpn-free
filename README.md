# Zaza VPN

Zaza VPN is a control plane and Telegram bot for free personal VPN subscriptions. It automatically processes public GitHub configuration files, health-checks parsed endpoints and exposes only active, highly rated configurations through device-scoped subscription URLs.

The bot's primary flow is a personal Zaza VPN connection page (`/connect?token=…`). It copies the subscription link and guides the user through importing it into **HAPP**, the primary client. HAPP receives the Wi-Fi and LTE auto-connect entries first, followed by the selected servers.

It is **not** a VPN data-plane and does not deploy VPN servers. When sources are third-party public configurations, a copied imported configuration cannot be revoked at its server; device slots protect only the subscription URLs.

## Start

1. Copy `.env.example` to `.env` and replace every secret.
2. Set `TELEGRAM_BOT_TOKEN`, `WEB_APP_BASE_URL`, and add the bot as an administrator to every required channel.
3. Run `docker compose up --build`.
4. Open `http://localhost:5173`; use `INITIAL_ADMIN_LOGIN` and `INITIAL_ADMIN_PASSWORD`.

For production, `WEB_APP_BASE_URL` must be the public **HTTPS** address of the frontend. It is embedded in the bot QR code and button. A device token is a credential: do not put it in analytics, logs, or third-party widgets.

## User connection flow

1. In Telegram, the user taps **«Получить VPN»**.
2. The bot sends a small QR code and **«Открыть Zaza VPN»**.
3. The Zaza page copies the device-scoped subscription and sends the user to the HAPP download page.
4. In HAPP the user chooses **«+» → «Импорт по ссылке»**, pastes the copied value, and connects.

HAPP is the primary client. Its published instructions describe importing a copied subscription link, so the project uses that reliable flow rather than inventing an unverified `happ://` deep link.

The first source must be a public GitHub `blob` or `raw.githubusercontent.com` URL to a text file containing supported URI configuration lines.

## Supported URI protocols

VLESS, Shadowsocks, Trojan, VMess, Hysteria2 and TUIC.

## Schedules

- GitHub source refresh: every 40 minutes.
- Active-node health checks: every 10 minutes.
- Telegram membership revalidation: every 12 hours.

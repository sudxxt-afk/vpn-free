from html import escape
from html.parser import HTMLParser
from urllib.parse import urlparse


ALLOWED_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "code", "pre", "blockquote", "tg-spoiler", "span", "a",
}


def _safe_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https", "tg"} and bool(parsed.netloc or parsed.scheme == "tg")


class _TelegramHTMLSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag not in ALLOWED_TAGS:
            return
        rendered = tag
        attributes = dict(attrs)
        suffix = ""
        if tag == "a":
            href = (attributes.get("href") or "").strip()
            if not _safe_url(href):
                return
            suffix = f' href="{escape(href, quote=True)}"'
        elif tag == "span":
            if attributes.get("class") != "tg-spoiler":
                return
            suffix = ' class="tg-spoiler"'
        self.parts.append(f"<{rendered}{suffix}>")
        self.stack.append(rendered)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.stack and self.stack[-1] == tag:
            self.parts.append(f"</{tag}>")
            self.stack.pop()

    def handle_data(self, data: str) -> None:
        self.parts.append(escape(data))

    def handle_entityref(self, name: str) -> None:
        self.parts.append(f"&amp;{escape(name)};")

    def handle_charref(self, name: str) -> None:
        self.parts.append(f"&amp;#{escape(name)};")

    def result(self) -> str:
        while self.stack:
            self.parts.append(f"</{self.stack.pop()}>")
        return "".join(self.parts).strip()


def sanitize_telegram_html(value: str) -> str:
    parser = _TelegramHTMLSanitizer()
    parser.feed(value)
    parser.close()
    return parser.result()

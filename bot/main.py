"""Entry point for the Telegram bot."""
from __future__ import annotations

import logging
from typing import Awaitable, Dict, Iterable, List, Protocol, Sequence, Tuple, cast
from urllib.parse import urljoin, urlparse

from telegram import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from db import (
    Category,
    CategoryItem,
    MSkladLink,
    PricingRule,
    Product,
    RuleType,
    Site,
    session_scope,
)
from db.session import init_database
from pricing.config import settings
from pricing.service import PriceMonitorService
from scraper import ScraperService

LOGGER = logging.getLogger(__name__)


class MessageEditor(Protocol):
    def edit_message_text(
        self, text: str, *args, **kwargs
    ) -> Awaitable[Message | bool]:
        """Edit message content within Telegram."""


def _require_message(update: Update) -> Message | None:
    message = update.effective_message
    if message is None:
        LOGGER.warning("Update %s does not contain a message", update.update_id)
    return message

SUPPORTED_SITES: Dict[str, str] = {
    "moscow.petrovich.ru": "petrovich",
    "whitehills.ru": "whitehills",
    "www.whitehills.ru": "whitehills",
    "mk4s.ru": "mk4s",
    "www.mk4s.ru": "mk4s",
}



def ensure_site(session, url: str) -> Site:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    adapter = SUPPORTED_SITES.get(host)
    if not adapter:
        raise ValueError(f"No parser configured for host {host}")
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    site = session.query(Site).filter_by(base_url=base_url).one_or_none()
    if site:
        return site
    site = Site(base_url=base_url, name=host, parser_adapter=adapter)
    session.add(site)
    session.flush()
    return site


def parse_rule_expression(expression: str) -> Tuple[RuleType, float]:
    expression = expression.strip()
    if expression.endswith("%"):
        value = float(expression.rstrip("%"))
        return RuleType.PERCENT_MARKUP, value
    if expression.startswith("-"):
        value = float(expression.lstrip("-"))
        return RuleType.MINUS_FIXED, value
    if expression.startswith("="):
        return RuleType.EQUAL, 0.0
    raise ValueError(f"Cannot parse rule expression '{expression}'")


def parse_rules(arguments: Iterable[str]) -> List[PricingRule]:
    rules: List[PricingRule] = []
    for arg in arguments:
        if "=" not in arg:
            raise ValueError("Rule must be specified as price_type=expression")
        price_type, expr = arg.split("=", 1)
        rule_type, value = parse_rule_expression(expr)
        rules.append(
            PricingRule(
                rule_type=rule_type,
                value=value,
                price_type=price_type.replace("_", " "),
            )
        )
    return rules


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = _require_message(update)
    if message is None:
        return
    await message.reply_text(
        "Добро пожаловать! Используйте /add_product <url> <код> <тип=правило>..."
    )


async def add_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = _require_message(update)
    if message is None:
        return
    args = list(context.args or [])
    if len(args) < 2:
        await message.reply_text(
            "Использование: /add_product <url> <код МойСклад> [<тип=правило> ...]"
        )
        return

    url = args[0]
    code = args[1]
    rule_args = args[2:]
    try:
        with session_scope() as session:
            site = ensure_site(session, url)
            product = Product(site=site, competitor_url=url)
            session.add(product)
            session.flush()

            rules = parse_rules(rule_args) if rule_args else []
            for rule in rules:
                rule.product_id = product.id
                session.add(rule)

            price_types = [rule.price_type for rule in rules] or settings.default_price_types
            link = MSkladLink(product_id=product.id, msklad_code=code, price_types=price_types)
            session.add(link)
            session.flush()
            product_id = product.id
        await message.reply_text(f"Товар добавлен с id={product_id}")
    except Exception as exc:  # pragma: no cover - runtime validation
        LOGGER.exception("Failed to add product")
        await message.reply_text(f"Ошибка: {exc}")


async def add_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = _require_message(update)
    if message is None:
        return
    args = list(context.args or [])
    if not args:
        await message.reply_text("Использование: /add_category <url категории>")
        return
    url = args[0]
    try:
        with session_scope() as session:
            site = ensure_site(session, url)
            scraper = ScraperService()
            snapshots = await scraper.fetch_category(site.parser_adapter, url)
            category = (
                session.query(Category)
                .filter_by(site_id=site.id, category_url=url)
                .one_or_none()
            )
            if not category:
                category = Category(site_id=site.id, category_url=url)
                session.add(category)
                session.flush()

            count = 0
            for snap in snapshots:
                full_url = snap.url if snap.url.startswith("http") else urljoin(url, snap.url)
                product = (
                    session.query(Product)
                    .filter_by(site_id=site.id, competitor_url=full_url)
                    .one_or_none()
                )
                if not product:
                    product = Product(
                        site=site,
                        competitor_url=full_url,
                        title=snap.title,
                        last_price=snap.price,
                    )
                    session.add(product)
                    session.flush()
                else:
                    if snap.title and not product.title:
                        product.title = snap.title
                if (
                    session.query(CategoryItem)
                    .filter_by(category_id=category.id, product_id=product.id)
                    .count()
                    == 0
                ):
                    session.add(CategoryItem(category_id=category.id, product_id=product.id))
                count += 1

        text_lines = [f"Найдено {count} товаров и сохранено в категории."]
        for snap in snapshots[:10]:
            text_lines.append(f"• {snap.title or snap.url} — {snap.price} ₽")
        if len(snapshots) > 10:
            text_lines.append("…")
        text_lines.append("Назначьте коды через /set_price_types и правила через /set_rules.")
        await message.reply_text("\n".join(text_lines))
    except Exception as exc:  # pragma: no cover
        LOGGER.exception("Failed to add category")
        await message.reply_text(f"Ошибка: {exc}")


async def set_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = _require_message(update)
    if message is None:
        return
    args = list(context.args or [])
    if len(args) < 2:
        await message.reply_text("Использование: /set_rules <id> <тип=правило> ...")
        return
    product_id = int(args[0])
    rule_args = args[1:]
    try:
        rules = parse_rules(rule_args)
    except ValueError as exc:
        await message.reply_text(str(exc))
        return

    with session_scope() as session:
        product = session.get(Product, product_id)
        if not product:
            await message.reply_text("Товар не найден")
            return
        session.query(PricingRule).filter_by(product_id=product_id).delete(synchronize_session=False)
        for rule in rules:
            rule.product_id = product_id
            session.add(rule)
    await message.reply_text("Правила обновлены")


async def set_price_types(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = _require_message(update)
    if message is None:
        return
    args = list(context.args or [])
    if len(args) < 3:
        await message.reply_text(
            "Использование: /set_price_types <id> <код МойСклад> <тип1> [тип2 ...]"
        )
        return
    product_id = int(args[0])
    code = args[1]
    price_types = [arg.replace("_", " ") for arg in args[2:]]
    with session_scope() as session:
        product = session.get(Product, product_id)
        if not product:
            await message.reply_text("Товар не найден")
            return
        links = cast(Sequence[MSkladLink], product.links or [])
        link = links[0] if links else None
        if link:
            link.price_types = price_types
            link.msklad_code = code
        else:
            session.add(MSkladLink(product_id=product_id, msklad_code=code, price_types=price_types))
    await message.reply_text("Типы цен обновлены")


async def list_items(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = _require_message(update)
    if message is None:
        return
    with session_scope() as session:
        rows = [
            (
                product.id,
                product.competitor_url,
                float(product.last_price) if product.last_price is not None else None,
                len(cast(Sequence[PricingRule], product.pricing_rules or [])),
            )
            for product in session.query(Product).filter_by(enabled=True).all()
        ]
    if not rows:
        await message.reply_text("Список пуст")
        return
    messages = []
    keyboard: List[List[InlineKeyboardButton]] = []
    for product_id, url, last_price, rule_count in rows:
        price = f"{last_price:.2f}" if last_price is not None else "-"
        messages.append(f"#{product_id} — {url}\nЦена: {price}\nПравил: {rule_count}")
        keyboard.append(
            [
                InlineKeyboardButton("Проверить", callback_data=f"check:{product_id}"),
                InlineKeyboardButton("Отключить", callback_data=f"disable:{product_id}"),
            ]
        )
    await message.reply_text(
        "\n\n".join(messages),
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
    )


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query: CallbackQuery | None = update.callback_query
    if query is None:
        LOGGER.warning("Callback router invoked without a callback query")
        return
    await query.answer()
    data = query.data or ""
    if data.startswith("check:"):
        product_id = int(data.split(":", 1)[1])
        await perform_recheck(query, product_id)
    elif data.startswith("disable:"):
        product_id = int(data.split(":", 1)[1])
        with session_scope() as session:
            product = session.get(Product, product_id)
            if product:
                product.enabled = False
        await query.edit_message_text(f"Мониторинг товара #{product_id} отключен")


async def perform_recheck(query: MessageEditor, product_id: int) -> None:
    with session_scope() as session:
        product = session.get(Product, product_id)
        if not product:
            await query.edit_message_text("Товар не найден")
            return
        service = PriceMonitorService(session)
        event = await service.check_product(product)
        session.flush()
    if event:
        await query.edit_message_text(
            f"Цена обновлена: {event.old_price} → {event.new_price}"
        )
    else:
        await query.edit_message_text("Цена не изменилась")


async def test_notify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = _require_message(update)
    if message is None:
        return
    await message.reply_text("Тестовое уведомление: система работает")


async def recheck(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = _require_message(update)
    if message is None:
        return
    args = list(context.args or [])
    if not args:
        await message.reply_text("Использование: /recheck <id>")
        return
    product_id = int(args[0])

    class Dummy(MessageEditor):
        async def edit_message_text(self, text: str, *args, **kwargs) -> Message:
            return await message.reply_text(text)

    query = Dummy()
    await perform_recheck(query, product_id)


async def delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = _require_message(update)
    if message is None:
        return
    args = list(context.args or [])
    if not args:
        await message.reply_text("Использование: /delete <id>")
        return
    product_id = int(args[0])
    with session_scope() as session:
        product = session.get(Product, product_id)
        if product:
            product.enabled = False
    await message.reply_text(f"Мониторинг товара #{product_id} отключен")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    init_database()
    application = ApplicationBuilder().token(settings.telegram_bot_token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("add_product", add_product))
    application.add_handler(CommandHandler("add_category", add_category))
    application.add_handler(CommandHandler("set_rules", set_rules))
    application.add_handler(CommandHandler("set_price_types", set_price_types))
    application.add_handler(CommandHandler("list", list_items))
    application.add_handler(CommandHandler("test_notify", test_notify))
    application.add_handler(CommandHandler("recheck", recheck))
    application.add_handler(CommandHandler("delete", delete))
    application.add_handler(CallbackQueryHandler(callback_router))

    LOGGER.info("Starting bot polling")
    application.run_polling()


if __name__ == "__main__":
    main()

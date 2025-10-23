"""Entry point for the Telegram bot."""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from textwrap import dedent
from typing import Awaitable, Dict, Iterable, List, Protocol, Sequence, Tuple, cast
from urllib.parse import urljoin, urlparse

from telegram import CallbackQuery, Message, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
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
from msklad import MoySkladClient, MoySkladError
from pricing.config import settings
from pricing.service import PriceMonitorService
from scraper import PriceNotFoundError, ScraperError, ScraperService

LOGGER = logging.getLogger(__name__)


class MessageEditor(Protocol):
    def edit_message_text(
        self, text: str, *args, **kwargs
    ) -> Awaitable[Message | bool]:
        """Edit message content within Telegram."""
        ...


def _require_message(update: Update) -> Message | None:
    message = update.effective_message
    if message is None:
        LOGGER.warning("Update %s does not contain a message", update.update_id)
    return message


def _describe_user(user: object | None) -> str:
    if user is None:
        return "unknown"
    user_id = getattr(user, "id", "unknown")
    username = getattr(user, "username", None)
    full_name = getattr(user, "full_name", None)
    if username:
        return f"{user_id} ({username})"
    if full_name:
        return f"{user_id} ({full_name})"
    return str(user_id)


def _split_text_lines(lines: Iterable[str], limit: int = 4096) -> List[str]:
    chunks: List[str] = []
    current_lines: List[str] = []
    current_length = 0
    for line in lines:
        addition = len(line) + (1 if current_lines else 0)
        if current_lines and current_length + addition > limit:
            chunks.append("\n".join(current_lines))
            current_lines = []
            current_length = 0
        current_lines.append(line)
        current_length += len(line) + 1
    if current_lines:
        chunks.append("\n".join(current_lines))
    return chunks

SUPPORTED_SITES: Dict[str, str] = {
    "moscow.petrovich.ru": "petrovich",
    "whitehills.ru": "whitehills",
    "www.whitehills.ru": "whitehills",
    "mk4s.ru": "mk4s",
    "www.mk4s.ru": "mk4s",
}


PRICE_RULES_HELP = dedent(
    """
    Правила изменения цены:
    • «<тип>=10%» — увеличить цену на указанный процент.
    • «<тип>=-500» — уменьшить цену на фиксированную сумму.
    • «<тип>==» — установить цену, равную цене конкурента.
    Несколько правил можно передать через точку с запятой или отдельными аргументами.
    """
).strip()


USAGE_HELP = dedent(
    """
    Как добавить товар:
    1. Отправьте команду /add_product <url> <код> [<тип=правило> ...]
       или сообщение вида <url>;<код>;<тип=правило>[;...].
    2. Название типа цены должно совпадать с МойСклад. Список доступных типов — /price_types.
    3. Примеры:
       • https://example.ru/item;ABC123;Цена продажи=10%;Цена для интернет магазина==
       • /add_product https://example.ru/item ABC123 Цена_для_интернет_магазина==-500
    """
).strip()


PRICE_TYPES_CACHE: List[str] | None = None


async def get_price_type_names(force_refresh: bool = False) -> List[str]:
    """Return cached list of MoySklad price type names."""

    global PRICE_TYPES_CACHE
    if PRICE_TYPES_CACHE is not None and not force_refresh:
        return PRICE_TYPES_CACHE

    client = MoySkladClient()

    try:
        mapping = await asyncio.to_thread(client.get_price_type_mapping)
    except MoySkladError:  # pragma: no cover - network failure guard
        LOGGER.exception("Failed to load price types")
        PRICE_TYPES_CACHE = []
        return PRICE_TYPES_CACHE

    PRICE_TYPES_CACHE = sorted(mapping.keys())
    return PRICE_TYPES_CACHE



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


def parse_inline_product_payload(text: str) -> Tuple[str, str, List[str]]:
    """Parse ``<url>;<code>;<rule>...`` payloads from plain text messages."""

    if ";" not in text:
        raise ValueError("Message does not look like a product payload")

    parts = [segment.strip() for segment in text.split(";")]
    parts = [segment for segment in parts if segment]
    if len(parts) < 2:
        raise ValueError("Сообщение должно содержать ссылку и код через ';'")

    url, code = parts[0], parts[1]
    rule_args = parts[2:]
    return url, code, rule_args


def _unique_preserve_order(items: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    result: List[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def describe_rule(rule: PricingRule) -> str:
    """Return a human friendly rule description."""

    if rule.rule_type == RuleType.PERCENT_MARKUP:
        return f"{rule.price_type}: +{rule.value:g}%"
    if rule.rule_type == RuleType.MINUS_FIXED:
        return f"{rule.price_type}: -{rule.value:g}"
    if rule.rule_type == RuleType.EQUAL:
        return f"{rule.price_type}: = цене конкурента"
    return f"{rule.price_type}: неизвестное правило"


def build_product_added_message(
    product_id: int, price_types: Iterable[str], rules: Sequence[PricingRule]
) -> str:
    """Compose a human readable confirmation message."""

    price_types_list = list(price_types)
    lines = [f"Товар добавлен с id={product_id}."]
    if price_types_list:
        lines.append("Типы цен для синхронизации: " + ", ".join(price_types_list))
    if rules:
        lines.append("Активные правила:")
        lines.extend(f"• {describe_rule(rule)}" for rule in rules)
    else:
        lines.append("Правила не заданы — цены будут скопированы для указанных типов.")
    lines.append("Команды для изменений: /set_rules и /set_price_types.")
    return "\n".join(lines)


async def create_product_record(
    url: str, code: str, rules: List[PricingRule]
) -> Tuple[int, List[str]]:
    """Persist a product with optional pricing rules and return metadata."""

    price_types = _unique_preserve_order(rule.price_type for rule in rules)
    if not price_types:
        loaded = await get_price_type_names()
        price_types = loaded or settings.default_price_types or ["Цена продажи"]

    with session_scope() as session:
        site = ensure_site(session, url)
        product = Product(site=site, competitor_url=url)
        session.add(product)
        session.flush()

        for rule in rules:
            rule.product_id = product.id
            session.add(rule)

        link = MSkladLink(product_id=product.id, msklad_code=code, price_types=price_types)
        session.add(link)
        session.flush()
        product_id = product.id

    return product_id, price_types


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = _require_message(update)
    if message is None:
        return
    await message.reply_text(
        "Добро пожаловать!\n"
        f"{USAGE_HELP}\n\n"
        f"{PRICE_RULES_HELP}"
    )


async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = _require_message(update)
    if message is None:
        return
    await message.reply_text(f"{USAGE_HELP}\n\n{PRICE_RULES_HELP}")


async def add_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = _require_message(update)
    if message is None:
        return
    args = list(context.args or [])
    if len(args) < 2:
        await message.reply_text(
            "Использование: /add_product <url> <код МойСклад> [<тип=правило> ...]"
            f"\n\n{USAGE_HELP}\n\n{PRICE_RULES_HELP}"
        )
        return

    url = args[0]
    code = args[1]
    rule_args = args[2:]
    try:
        rules = parse_rules(rule_args) if rule_args else []
    except ValueError as exc:
        await message.reply_text(f"Ошибка: {exc}\n\n{PRICE_RULES_HELP}")
        return

    try:
        product_id, price_types = await create_product_record(url, code, rules)
    except Exception as exc:  # pragma: no cover - runtime validation
        LOGGER.exception("Failed to add product")
        await message.reply_text(f"Ошибка: {exc}")
        return

    await message.reply_text(build_product_added_message(product_id, price_types, rules))


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


async def price_types(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = _require_message(update)
    if message is None:
        return

    force_refresh = bool(context.args and context.args[0].lower() in {"refresh", "update"})
    names = await get_price_type_names(force_refresh=force_refresh)
    if not names:
        await message.reply_text("Не удалось получить список типов цен. Попробуйте позже.")
        return

    lines = ["Доступные типы цен МойСклад:"]
    lines.extend(f"• {name}" for name in names)
    if not force_refresh:
        lines.append("(Добавьте 'refresh' к команде для обновления списка из МойСклад.)")
    await message.reply_text("\n".join(lines))


async def handle_inline_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = _require_message(update)
    if message is None:
        return
    text = message.text or ""
    try:
        url, code, rule_args = parse_inline_product_payload(text)
    except ValueError:
        return

    try:
        rules = parse_rules(rule_args) if rule_args else []
    except ValueError as exc:
        await message.reply_text(f"Ошибка: {exc}\n\n{PRICE_RULES_HELP}")
        return

    try:
        product_id, price_types = await create_product_record(url, code, rules)
    except Exception as exc:  # pragma: no cover - runtime validation
        LOGGER.exception("Failed to add product from inline message")
        await message.reply_text(f"Ошибка: {exc}")
        return

    await message.reply_text(build_product_added_message(product_id, price_types, rules))


async def list_items(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = _require_message(update)
    if message is None:
        return
    with session_scope() as session:
        products = (
            session.query(Product)
            .filter_by(enabled=True)
            .order_by(Product.id)
            .all()
        )
    if not products:
        await message.reply_text("Список пуст")
        return
    user_label = _describe_user(getattr(message, "from_user", None))
    LOGGER.info("Sending product list", extra={"count": len(products), "user": user_label})

    lines: List[str] = ["Ваши товары:"]
    for index, product in enumerate(products, start=1):
        title = product.title or product.competitor_url
        if product.last_price is not None:
            price_value = Decimal(product.last_price).quantize(Decimal("0.01"))
            price_text = f"{price_value:.2f}"
        else:
            price_text = "-"
        lines.append(f"{index}) {title} — {price_text} — ID: {product.id}")
        lines.append(
            f"   Команды: /check {product.id}   /disable {product.id}   /unlink {product.id}"
        )

    for chunk in _split_text_lines(lines):
        await message.reply_text(chunk)


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
        try:
            event = await service.check_product(product)
        except PriceNotFoundError as exc:
            LOGGER.warning(
                "Manual recheck price not found",
                extra={
                    "product_id": product_id,
                    "url": product.competitor_url,
                    "reason": str(exc),
                },
            )
            await query.edit_message_text(f"Не удалось проверить товар: {exc}")
            return
        except ScraperError as exc:
            LOGGER.exception("Failed to fetch competitor price for product %s", product_id)
            await query.edit_message_text(f"Не удалось проверить товар: {exc}")
            return
        except MoySkladError as exc:
            LOGGER.exception("Failed to push updated price to MoySklad for product %s", product_id)
            await query.edit_message_text(f"Не удалось обновить цену в МойСклад: {exc}")
            return
        except Exception as exc:  # pragma: no cover - unexpected runtime issues
            LOGGER.exception("Unexpected error during manual recheck for product %s", product_id)
            await query.edit_message_text(f"Ошибка при проверке товара: {exc}")
            return
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


async def unlink(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = _require_message(update)
    if message is None:
        return
    args = list(context.args or [])
    if not args:
        await message.reply_text("Использование: /unlink <ID товара>")
        return
    try:
        product_id = int(args[0])
    except ValueError:
        await message.reply_text("ID товара должен быть числом.")
        return

    try:
        with session_scope() as session:
            product = session.get(Product, product_id)
            if not product:
                await message.reply_text(
                    f"Товар с ID {product_id} не найден или не принадлежит вам."
                )
                return
            product.enabled = False
            removed_links = 0
            for link in list(product.links):
                session.delete(link)
                removed_links += 1
        LOGGER.info(
            "Product unlinked",
            extra={"product_id": product_id, "links_removed": removed_links},
        )
        await message.reply_text(f"Товар {product_id} отвязан. Мониторинг остановлен.")
    except Exception:  # pragma: no cover - unexpected runtime issues
        LOGGER.exception("Failed to unlink product", extra={"product_id": product_id})
        await message.reply_text("Не удалось отвязать товар. Попробуйте позже.")


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
    application.add_handler(CommandHandler("help", show_help))
    application.add_handler(CommandHandler("add_product", add_product))
    application.add_handler(CommandHandler("add_category", add_category))
    application.add_handler(CommandHandler("set_rules", set_rules))
    application.add_handler(CommandHandler("set_price_types", set_price_types))
    application.add_handler(CommandHandler("price_types", price_types))
    application.add_handler(CommandHandler("list", list_items))
    application.add_handler(CommandHandler("test_notify", test_notify))
    application.add_handler(CommandHandler("recheck", recheck))
    application.add_handler(CommandHandler("unlink", unlink))
    application.add_handler(CommandHandler("delete", delete))
    application.add_handler(CallbackQueryHandler(callback_router))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_inline_product))

    LOGGER.info("Starting bot polling")
    application.run_polling()


if __name__ == "__main__":
    main()

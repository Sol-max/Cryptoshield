"""
CryptoShield AI — Telegram Bot
Принимает скам-сообщения, извлекает крипто-адреса, сохраняет в базу.
"""

import os
import json
import logging
import asyncio
from datetime import datetime
from chain_tracer import ChainTracer
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
import anthropic

from database import Database
from analyzer import ScamAnalyzer

# Логирование
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Инициализация
db = Database("scam_reports.db")
analyzer = ScamAnalyzer(api_key=os.environ["ANTHROPIC_API_KEY"])

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY", "YourApiKeyToken")
ADMIN_ID = int(os.environ["ADMIN_ID"])
DB_PATH = "scam_reports.db"


# ─── Команды ────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🛡 *CryptoShield AI*\n\n"
        "Помогаю бороться с крипто-мошенниками.\n\n"
        "*Что я умею:*\n"
        "• Извлекаю адреса кошельков из скам-сообщений\n"
        "• Проверяю адреса по базе мошенников\n"
        "• Анализирую подозрительные транзакции\n\n"
        "*Как использовать:*\n"
        "1️⃣ Перешли мне скам-сообщение которое получил\n"
        "2️⃣ Или напиши /check `адрес` чтобы проверить кошелёк\n"
        "3️⃣ /stats — статистика базы\n\n"
        "Каждый твой репорт помогает защитить других людей 💪"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет SQLite базу админу."""
    user_id = update.effective_user.id

    if user_id != ADMIN_ID:
        await update.message.reply_text("⛔ Access denied")
        return

    try:
        with open(DB_PATH, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename="scam_reports.db",
                caption="📦 SQLite backup"
            )
    except Exception as e:
        await update.message.reply_text(f"Ошибка backup: {e}")
        
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 *Помощь*\n\n"
        "/start — главное меню\n"
        "/check `адрес` — проверить адрес кошелька\n"
        "/stats — статистика базы\n"
        "/top — топ-10 адресов с наибольшим числом жалоб\n\n"
        "*Форматы адресов которые я понимаю:*\n"
        "• Bitcoin: начинается с `1`, `3` или `bc1`\n"
        "• Ethereum/BSC: начинается с `0x`\n"
        "• Tron: начинается с `T`\n"
        "• Solana: 32–44 символа base58\n"
        "• TON: начинается с `EQ` или `UQ`\n\n"
        "Просто перешли подозрительное сообщение — я сам найду адреса."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = db.get_stats()
    text = (
        f"📊 *Статистика CryptoShield*\n\n"
        f"🔴 Адресов в базе: *{stats['total_addresses']}*\n"
        f"📋 Репортов получено: *{stats['total_reports']}*\n"
        f"👥 Участников: *{stats['total_users']}*\n"
        f"📅 Новых сегодня: *{stats['today_reports']}*\n\n"
        f"Топ сеть: *{stats['top_network']}*"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    top = db.get_top_addresses(10)
    if not top:
        await update.message.reply_text("База пока пуста. Присылай скам-сообщения!")
        return

    lines = ["🔴 *Топ мошеннических адресов:*\n"]
    for i, row in enumerate(top, 1):
        addr = row["address"]
        short = addr[:8] + "..." + addr[-6:]
        lines.append(
            f"{i}. `{short}`\n"
            f"   Жалоб: {row['report_count']} | "
            f"Score: {row['risk_score']}/100 | "
            f"{row['network']}"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Укажи адрес: /check `0x1234...abcd`",
            parse_mode="Markdown"
        )
        return

    address = context.args[0].strip()
    await _check_address(update, address)


# ─── Обработка сообщений ─────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Основной обработчик — анализирует любое входящее сообщение."""
    message = update.message
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name

    # Получаем текст (включая пересланные сообщения)
    text = ""
    if message.text:
        text = message.text
    elif message.caption:
        text = message.caption
    elif message.forward_date:
        # Пересланное без текста
        await message.reply_text(
            "Получил пересланное сообщение, но оно пустое. "
            "Попробуй скопировать текст вручную."
        )
        return

    if len(text) < 10:
        await message.reply_text(
            "Сообщение слишком короткое. Перешли скам-письмо или напиши /help"
        )
        return

    # Показываем что работаем
    wait_msg = await message.reply_text("🔍 Анализирую сообщение...")

    try:
        result = await analyzer.analyze(text, user_id=user_id, username=username)

        if not result["addresses"]:
            await wait_msg.edit_text(
                "🔎 Крипто-адресов в сообщении не найдено.\n\n"
                "Если там есть адрес — попробуй скопировать только его "
                "и отправить командой /check `адрес`",
                parse_mode="Markdown"
            )
            return

        # Сохраняем в базу
        report_id = db.save_report(
            user_id=user_id,
            username=username,
            original_text=text,
            result=result
        )
        # On-chain tracing
        try:
            async with ChainTracer(ETHERSCAN_API_KEY) as tracer:
                total_related = 0

                for addr_info in result["addresses"]:
                    address = addr_info["address"]
                    network = addr_info.get("network", "Unknown")

                    related = await tracer.trace_all(address, network)

                    if related:
                        db.save_related_addresses(related)
                        total_related += len(related)

                logger.info(f"Найдено связанных адресов: {total_related}")

        except Exception as e:
            logger.warning(f"Ошибка chain tracing: {e}")    
        # Формируем ответ
        response = _format_analysis_result(result, report_id)
        keyboard = _make_report_keyboard(result["addresses"], report_id)

        await wait_msg.edit_text(
            response,
            parse_mode="Markdown",
            reply_markup=keyboard
        )

    except Exception as e:
        logger.error(f"Ошибка анализа: {e}")
        await wait_msg.edit_text(
            "⚠️ Произошла ошибка при анализе. Попробуй позже."
        )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка кнопок."""
    query = update.callback_query
    await query.answer()

    data = query.data
    if data.startswith("check_"):
        address = data[6:]
        await _check_address(query, address)
    elif data.startswith("confirm_"):
        report_id = int(data[8:])
        db.confirm_report(report_id, query.from_user.id)
        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Подтверждено, спасибо!", callback_data="done")
            ]])
        )
    elif data == "done":
        pass


# ─── Вспомогательные функции ─────────────────────────────────────────────────

async def _check_address(update_or_query, address: str):
    """Проверяет адрес по базе и выводит результат."""
    record = db.get_address(address)

    # Определяем сеть
    network = _detect_network(address)

    if record:
        risk = record["risk_score"]
        emoji = "🔴" if risk >= 70 else "🟡" if risk >= 40 else "🟢"
        text = (
            f"{emoji} *Адрес найден в базе*\n\n"
            f"`{address}`\n\n"
            f"Сеть: {network}\n"
            f"Жалоб: *{record['report_count']}*\n"
            f"Risk Score: *{risk}/100*\n"
            f"Тип скама: {record['scam_type'] or 'не определён'}\n"
            f"Первый репорт: {record['first_seen'][:10]}\n\n"
            f"⚠️ _Не отправляй средства на этот адрес_"
        )
    else:
        text = (
            f"✅ *Адрес не найден в базе*\n\n"
            f"`{address}`\n\n"
            f"Сеть: {network}\n\n"
            f"_Это не означает что адрес безопасен — "
            f"база обновляется постоянно._"
        )

    if hasattr(update_or_query, "message"):
        await update_or_query.message.reply_text(text, parse_mode="Markdown")
    else:
        await update_or_query.edit_message_text(text, parse_mode="Markdown")


def _detect_network(address: str) -> str:
    if address.startswith(("1", "3", "bc1")):
        return "Bitcoin (BTC)"
    elif address.startswith("0x") and len(address) == 42:
        return "Ethereum / BSC / Polygon"
    elif address.startswith("T") and len(address) == 34:
        return "Tron (TRX)"
    elif address.startswith(("EQ", "UQ")):
        return "TON"
    elif len(address) in range(32, 45):
        return "Solana (SOL)"
    return "Неизвестная сеть"


def _format_analysis_result(result: dict, report_id: int) -> str:
    addrs = result["addresses"]
    scam_type = result.get("scam_type", "неизвестный")
    summary = result.get("summary", "")

    lines = [f"🛡 *Анализ завершён* (репорт #{report_id})\n"]

    if summary:
        lines.append(f"_{summary}_\n")

    lines.append(f"🔍 Тип скама: *{scam_type}*")
    lines.append(f"📍 Найдено адресов: *{len(addrs)}*\n")

    for addr_info in addrs:
        addr = addr_info["address"]
        network = addr_info.get("network", _detect_network(addr))
        in_db = addr_info.get("already_in_db", False)
        status = "🔴 уже в базе!" if in_db else "🆕 добавлен в базу"

        lines.append(f"`{addr[:20]}...`")
        lines.append(f"  Сеть: {network} | {status}")

    lines.append("\n✅ *Спасибо! Твой репорт помогает защитить других.*")
    lines.append("Если видел похожий скам — нажми «Подтвердить».")

    return "\n".join(lines)


def _make_report_keyboard(addresses: list, report_id: int):
    buttons = []
    for addr_info in addresses[:2]:  # Максимум 2 кнопки
        addr = addr_info["address"]
        short = addr[:10] + "..."
        buttons.append(
            InlineKeyboardButton(
                f"🔎 Проверить {short}",
                callback_data=f"check_{addr}"
            )
        )

    bottom = [InlineKeyboardButton("✅ Подтвердить — я тоже пострадал", callback_data=f"confirm_{report_id}")]

    keyboard = []
    if buttons:
        keyboard.append(buttons)
    keyboard.append(bottom)

    return InlineKeyboardMarkup(keyboard)

async def graph_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Использование: /graph `адрес`",
            parse_mode="Markdown"
        )
        return

    address = context.args[0].strip()
    record = db.get_address(address)

    if not record:
        await update.message.reply_text("Адрес не найден в базе.")
        return

    text = (
        f"🕸 *Graph lookup*\n\n"
        f"`{address}`\n"
        f"Сеть: {record['network']}\n"
        f"Risk: {record['risk_score']}/100\n"
        f"Жалоб: {record['report_count']}\n\n"
        f"Используй /check для детальной проверки."
    )

    await update.message.reply_text(text, parse_mode="Markdown")
# ─── Запуск ──────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("top", top_command))
    app.add_handler(CommandHandler("check", check_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CommandHandler("graph", graph_command))
    app.add_handler(CommandHandler("backup", backup_command))
    logger.info("🛡 CryptoShield Bot запущен")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

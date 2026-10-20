import asyncio
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from pypdf import PdfReader, PdfWriter

# ---------- Logging ----------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------- Concurrency control ----------
pdf_semaphore = asyncio.Semaphore(2)  # max 2 concurrent PDF jobs

# ---------- Helpers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a welcome message and usage instructions."""
    text = (
        "📄 **PDF Tools Bot**\n\n"
        "Send me a PDF file and choose an action:\n"
        "• **Merge** – combine multiple PDFs (send files one by one, then /merge)\n"
        "• **Split** – extract pages (e.g., 1-3,5)\n"
        "• **Compress** – reduce file size with Ghostscript\n\n"
        "Commands:\n"
        "/start – show this message\n"
        "/help – same as /start\n"
        "/cancel – cancel current operation\n\n"
        "⚠️ Telegram limits bot downloads to 20 MB."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear any stored files and reset the session."""
    context.user_data.clear()
    await update.message.reply_text("✅ Session cleared. Send a new PDF to begin.")


async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Entry point for any PDF document sent by the user."""
    async with pdf_semaphore:
        try:
            document = update.message.document
            if document.file_size > 20 * 1024 * 1024:
                await update.message.reply_text(
                    "❌ File too large. Telegram bots can only download files up to 20 MB."
                )
                return

            # Download to a temporary directory unique to this update
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp_path = Path(tmpdir)
                input_path = tmp_path / "input.pdf"
                file = await document.get_file()
                await file.download_to_drive(custom_path=input_path)

                # Store path and metadata for later operations
                context.user_data["input_path"] = str(input_path)
                context.user_data["tmpdir"] = tmpdir
                context.user_data["original_name"] = document.file_name or "file.pdf"

                # Show action keyboard
                keyboard = [
                    [
                        InlineKeyboardButton("🔗 Merge", callback_data="merge"),
                        InlineKeyboardButton("✂️ Split", callback_data="split"),
                        InlineKeyboardButton("🗜️ Compress", callback_data="compress"),
                    ]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text(
                    "What would you like to do with this PDF?",
                    reply_markup=reply_markup,
                )
        except Exception as e:
            logger.exception("Error in handle_pdf")
            await update.message.reply_text("❌ Failed to download the PDF. Please try again.")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard actions."""
    query = update.callback_query
    await query.answer()

    action = query.data
    input_path = context.user_data.get("input_path")
    if not input_path or not os.path.exists(input_path):
        await query.edit_message_text("❌ No PDF found. Please send a PDF first.")
        return

    tmpdir = context.user_data["tmpdir"]
    original_name = context.user_data.get("original_name", "file.pdf")

    if action == "merge":
        await query.edit_message_text(
            "📥 **Merge mode**\n\n"
            "Send additional PDF files one by one. When you are ready, send /done to merge them all.",
        )
        context.user_data["merge_mode"] = True
        context.user_data["merge_files"] = [input_path]
        # Keep the original tmpdir alive for merge
        return

    if action == "split":
        await query.edit_message_text(
            "✂️ **Split mode**\n\n"
            "Send the page range you want to extract (e.g., `1-3,5`).\n"
            "Only those pages will be kept in a new PDF.",
        )
        context.user_data["awaiting_split_range"] = True
        return

    if action == "compress":
        await query.edit_message_text("🗜️ Compressing… please wait.")
        try:
            output_path = os.path.join(tmpdir, f"compressed_{original_name}")
            # Ghostscript compression – /ebook profile for a good balance
            cmd = [
                "gs",
                "-sDEVICE=pdfwrite",
                "-dCompatibilityLevel=1.4",
                "-dPDFSETTINGS=/ebook",
                "-dNOPAUSE",
                "-dQUIET",
                "-dBATCH",
                f"-sOutputFile={output_path}",
                input_path,
            ]
            subprocess.run(cmd, check=True, capture_output=True)
            await query.edit_message_text("✅ Compression complete. Sending file…")
            await query.message.reply_document(
                document=open(output_path, "rb"),
                filename=f"compressed_{original_name}",
            )
        except subprocess.CalledProcessError as e:
            logger.error(f"Ghostscript failed: {e.stderr.decode()}")
            await query.edit_message_text("❌ Compression failed. Ghostscript error.")
        except Exception:
            logger.exception("Compression error")
            await query.edit_message_text("❌ Compression failed.")
        finally:
            # Clean up after compression
            context.user_data.clear()


async def split_range_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Receive the page range for split mode."""
    if not context.user_data.get("awaiting_split_range"):
        return

    text = update.message.text.strip()
    try:
        # Parse ranges like "1-3,5"
        pages_to_keep = []
        for part in text.split(","):
            part = part.strip()
            if "-" in part:
                start, end = map(int, part.split("-"))
                pages_to_keep.extend(range(start - 1, end))  # 0‑indexed
            else:
                pages_to_keep.append(int(part) - 1)

        input_path = context.user_data["input_path"]
        tmpdir = context.user_data["tmpdir"]
        original_name = context.user_data.get("original_name", "file.pdf")

        reader = PdfReader(input_path)
        writer = PdfWriter()
        for idx in pages_to_keep:
            if 0 <= idx < len(reader.pages):
                writer.add_page(reader.pages[idx])

        output_path = os.path.join(tmpdir, f"split_{original_name}")
        with open(output_path, "wb") as f:
            writer.write(f)

        await update.message.reply_document(
            document=open(output_path, "rb"),
            filename=f"split_{original_name}",
        )
    except Exception as e:
        logger.exception("Split error")
        await update.message.reply_text(
            "❌ Invalid page range. Use a format like `1-3,5`."
        )
    finally:
        context.user_data.clear()


async def merge_collector(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Collect additional PDFs during merge mode."""
    if not context.user_data.get("merge_mode"):
        return

    document = update.message.document
    if not document or not document.file_name.lower().endswith(".pdf"):
        await update.message.reply_text("Please send a PDF file.")
        return

    tmpdir = context.user_data["tmpdir"]
    file_path = os.path.join(tmpdir, f"merge_{len(context.user_data['merge_files'])}.pdf")
    file = await document.get_file()
    await file.download_to_drive(custom_path=file_path)
    context.user_data["merge_files"].append(file_path)
    await update.message.reply_text(
        f"✅ Added ({len(context.user_data['merge_files'])} files total). Send more or /done to merge."
    )


async def done_merge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Execute the merge when the user sends /done."""
    if not context.user_data.get("merge_mode"):
        await update.message.reply_text("Not in merge mode.")
        return

    files = context.user_data.get("merge_files", [])
    if len(files) < 2:
        await update.message.reply_text("Need at least 2 PDFs to merge.")
        return

    try:
        writer = PdfWriter()
        for path in files:
            writer.append(path)

        tmpdir = context.user_data["tmpdir"]
        output_path = os.path.join(tmpdir, "merged.pdf")
        with open(output_path, "wb") as f:
            writer.write(f)

        await update.message.reply_document(
            document=open(output_path, "rb"),
            filename="merged.pdf",
        )
    except Exception:
        logger.exception("Merge error")
        await update.message.reply_text("❌ Merge failed.")
    finally:
        context.user_data.clear()


# ---------- Main ----------
def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN environment variable is not set.")

    application = Application.builder().token(token).build()

    # Commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("done", done_merge))

    # PDF document handler (initial upload)
    application.add_handler(
        MessageHandler(filters.Document.PDF & ~filters.COMMAND, handle_pdf)
    )

    # Merge collector (only active when merge_mode is True)
    # Note: handle_pdf and merge_collector both listen to PDFs.
    # We need to route: if merge_mode is True, use merge_collector; else handle_pdf.
    # Easiest: combine logic in one handler.
    # We'll replace the two handlers with a single dispatcher.

    # Remove the above two handlers and add a unified PDF handler:
    # (We'll re‑add after defining a combined function.)

    # -- Re‑define unified PDF handler --
    async def unified_pdf_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if context.user_data.get("merge_mode"):
            await merge_collector(update, context)
        else:
            await handle_pdf(update, context)

    # Clear previous handlers and add unified one
    application.handlers.clear()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("done", done_merge))
    application.add_handler(
        MessageHandler(filters.Document.PDF & ~filters.COMMAND, unified_pdf_handler)
    )
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, split_range_handler)
    )

    logger.info("Bot starting in polling mode…")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

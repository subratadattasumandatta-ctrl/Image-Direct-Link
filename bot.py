import os
import asyncio
import requests
import logging
import time
import threading
from collections import defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TimedOut, NetworkError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.request import HTTPXRequest

# Logging setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# =============================================
# BOT TOKEN
# =============================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN environment variable set nahi hai!")


# =============================================
# BULK IMAGE GROUPING
# =============================================
pending_groups = {}
pending_singles = defaultdict(list)
SINGLE_WAIT = 3


# =============================================
# HEALTH SERVER — Render ke liye zaroori
# =============================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot chal raha hai! OK")

    def log_message(self, format, *args):
        pass  # Logs quiet rakhne ke liye


def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info(f"✅ Health server port {port} par chalu!")
    server.serve_forever()


# =============================================
# CATBOX.MOE UPLOAD FUNCTION (Anonymous, no API key)
# =============================================
def upload_to_catbox(image_bytes: bytes, filename: str, expiry_seconds: int = 0) -> dict:
    # Note: Catbox.moe links hamesha PERMANENT hote hain.
    # expiry_seconds parameter yahan sirf compatibility ke liye rakha gaya hai, uska koi effect nahi.
    try:
        url = "https://catbox.moe/user/api.php"
        files = {
            "fileToUpload": (filename, image_bytes),
        }
        data = {
            "reqtype": "fileupload",
        }

        response = requests.post(url, data=data, files=files, timeout=60)

        # Debug logging — logs mein pura response dikhega
        logger.info(f"Catbox response status: {response.status_code}")
        logger.info(f"Catbox response body: {response.text[:500]}")

        if response.status_code == 200:
            direct_link = response.text.strip()

            # Catbox success par ek direct URL string return karta hai (https://files.catbox.moe/xxxx.jpg)
            if direct_link.startswith("https://files.catbox.moe/"):
                return {
                    "success": True,
                    "direct_link": direct_link,
                    "view_link": direct_link,
                    "delete_link": "Catbox par manual delete: https://catbox.moe/user/manage.php",
                    "size": len(image_bytes),
                }
            else:
                return {"success": False, "error": direct_link[:300] or "Unknown Catbox error"}
        else:
            return {"success": False, "error": f"Server error: {response.status_code} - {response.text[:300]}"}

    except requests.exceptions.Timeout:
        return {"success": False, "error": "Catbox timeout!"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# =============================================
# EXPIRY OPTIONS
# =============================================
EXPIRY_OPTIONS = {
    "permanent": ("♾️ Permanent", 0),
    "1hour": ("⏰ 1 Ghanta", 3600),
    "1day": ("📅 1 Din", 86400),
    "1week": ("📆 1 Hafta", 604800),
    "1month": ("🗓️ 1 Mahina", 2592000),
}


# =============================================
# EXPIRY KEYBOARD
# =============================================
def get_expiry_keyboard(prefix: str) -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("♾️ Permanent", callback_data=f"{prefix}_permanent"),
            InlineKeyboardButton("⏰ 1 Ghanta", callback_data=f"{prefix}_1hour"),
        ],
        [
            InlineKeyboardButton("📅 1 Din", callback_data=f"{prefix}_1day"),
            InlineKeyboardButton("📆 1 Hafta", callback_data=f"{prefix}_1week"),
        ],
        [
            InlineKeyboardButton("🗓️ 1 Mahina", callback_data=f"{prefix}_1month"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


# =============================================
# FILE DOWNLOAD WITH RETRY
# =============================================
async def download_file(file_path: str, retries: int = 3) -> bytes | None:
    for attempt in range(retries):
        try:
            img_response = requests.get(file_path, timeout=60)
            if img_response.status_code == 200:
                return img_response.content
        except Exception as e:
            logger.warning(f"Download attempt {attempt + 1} fail: {e}")
        if attempt < retries - 1:
            await asyncio.sleep(5)
    return None


# =============================================
# SEND MESSAGE WITH RETRY
# =============================================
async def safe_reply(message, text, parse_mode=None, reply_markup=None):
    for attempt in range(3):
        try:
            return await message.reply_text(
                text,
                parse_mode=parse_mode,
                reply_markup=reply_markup
            )
        except (TimedOut, NetworkError):
            if attempt == 2:
                logger.error("safe_reply fail")
                return None
            await asyncio.sleep(3)


async def safe_edit(message, text, parse_mode=None):
    for attempt in range(3):
        try:
            return await message.edit_text(text, parse_mode=parse_mode)
        except (TimedOut, NetworkError):
            if attempt == 2:
                logger.error("safe_edit fail")
                return None
            await asyncio.sleep(3)


# =============================================
# BULK UPLOAD PROCESSOR
# =============================================
async def process_bulk_upload(
    images: list,
    expiry_val: int,
    expiry_label: str,
    status_msg,
    original_message,
):
    total = len(images)
    results = []

    await safe_edit(status_msg, f"⏳ Upload shuru... 0/{total} complete")

    for i, img_info in enumerate(images, 1):
        image_bytes = await download_file(img_info["file_path"])

        if not image_bytes:
            results.append({
                "index": i,
                "filename": img_info["filename"],
                "success": False,
                "error": "Download fail"
            })
            await safe_edit(status_msg, f"⏳ Processing... {i}/{total} ❌ #{i} download fail")
            continue

        result = upload_to_catbox(image_bytes, img_info["filename"], expiry_val)
        result["index"] = i
        result["filename"] = img_info["filename"]
        results.append(result)

        done = sum(1 for r in results if r.get("success"))
        failed = sum(1 for r in results if not r.get("success"))
        await safe_edit(
            status_msg,
            f"⏳ Upload ho rahi hai...\n"
            f"✅ {done} done | ❌ {failed} fail | 🔄 {total - i} baki\n"
            f"({i}/{total})"
        )

        if i < total:
            await asyncio.sleep(1)

    success_results = [r for r in results if r.get("success")]
    fail_results = [r for r in results if not r.get("success")]

    if not success_results:
        await safe_edit(status_msg, f"❌ Koi bhi image upload nahi hui!\n\nErrors:\n" +
                        "\n".join([f"#{r['index']}: {r['error']}" for r in fail_results]))
        return

    msg_lines = [f"✅ *{len(success_results)}/{total} Images Upload Successful!*",
                 f"⏰ *Expiry:* {expiry_label}\n"]

    for r in success_results:
        size_kb = r.get("size", 0) // 1024
        msg_lines.append(
            f"🖼️ *Image #{r['index']}*\n"
            f"🔗 `{r['direct_link']}`\n"
            f"📦 {size_kb} KB"
        )

    if fail_results:
        msg_lines.append(f"\n❌ *{len(fail_results)} images fail huin:*")
        for r in fail_results:
            msg_lines.append(f"#{r['index']}: {r['error']}")

    msg_lines.append("\n💡 *HTML mein use karo:*")
    for r in success_results:
        msg_lines.append(f"`<img src=\"{r['direct_link']}\">`")

    full_msg = "\n".join(msg_lines)

    if len(full_msg) > 4000:
        summary = (
            f"✅ *{len(success_results)}/{total} Images Upload Successful!*\n"
            f"⏰ *Expiry:* {expiry_label}\n\n"
            f"Neeche har image ka link alag alag diya gaya hai 👇"
        )
        await safe_edit(status_msg, summary, parse_mode="Markdown")

        for r in success_results:
            size_kb = r.get("size", 0) // 1024
            img_msg = (
                f"🖼️ *Image #{r['index']}*\n"
                f"🔗 *Direct Link:*\n`{r['direct_link']}`\n"
                f"👁️ *View:* {r['view_link']}\n"
                f"🗑️ *Delete:* `{r['delete_link']}`\n"
                f"📦 *Size:* {size_kb} KB\n"
                f"💡 `<img src=\"{r['direct_link']}\">`"
            )
            await safe_reply(original_message, img_msg, parse_mode="Markdown")
            await asyncio.sleep(0.5)
    else:
        await safe_edit(status_msg, full_msg, parse_mode="Markdown")


# =============================================
# ERROR HANDLER
# =============================================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    error = context.error
    logger.error(f"Error: {error}", exc_info=True)

    if isinstance(error, (TimedOut, NetworkError)):
        msg = "⚠️ Network timeout! Thodi der baad dobara try karein."
    else:
        msg = f"❌ Kuch gadbad ho gayi: {str(error)}"

    try:
        if update and hasattr(update, "message") and update.message:
            await update.message.reply_text(msg)
        elif update and hasattr(update, "callback_query") and update.callback_query:
            await update.callback_query.message.reply_text(msg)
    except Exception as e:
        logger.error(f"Error handler mein bhi error: {e}")


# =============================================
# /start COMMAND
# =============================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply(
        update.message,
        "👋 *Assalam o Alaikum!*\n\n"
        "Main aapka *Image Link Bot* hoon! 🤖\n\n"
        "📸 *Kaise use karein:*\n"
        "1. Ek ya zyada images bhejo (ek saath 10 tak!)\n"
        "2. Expiry time choose karo\n"
        "3. Har image ka direct link alag alag pao! 🔗\n\n"
        "✅ *Album support:* Telegram mein select karke ek saath bhejo\n\n"
        "⚠️ *Note:* Links hamesha *Permanent* rahenge (Catbox.moe permanent hosting deta hai)\n\n"
        "📌 Commands:\n"
        "/start — Bot start karo\n"
        "/help — Madad",
        parse_mode="Markdown"
    )


# =============================================
# /help COMMAND
# =============================================
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply(
        update.message,
        "🆘 *Help*\n\n"
        "➡️ *Single image:* Ek image bhejo → link pao\n"
        "➡️ *Bulk images:* Multiple images select karke ek saath bhejo → "
        "har image ka alag link pao\n\n"
        "📱 *Bulk kaise bhejein:*\n"
        "1. Telegram mein gallery open karo\n"
        "2. Multiple images select karo (hold karke)\n"
        "3. Ek saath send karo\n\n"
        "💡 *HTML use:*\n"
        "`<img src=\"YOUR_DIRECT_LINK\">`\n\n"
        "⚠️ *Limit:* Ek baar mein max 10 images\n"
        "⚠️ *Types:* JPG, PNG, GIF, WEBP",
        parse_mode="Markdown"
    )


# =============================================
# IMAGE HANDLER
# =============================================
async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user_id = message.from_user.id
    media_group_id = message.media_group_id

    for attempt in range(3):
        try:
            if message.photo:
                file = await message.photo[-1].get_file()
                filename = f"image_{int(time.time())}.jpg"
            elif message.document:
                doc = message.document
                if not doc.mime_type or not doc.mime_type.startswith("image/"):
                    await safe_reply(message, "❌ Sirf image files bhejein!")
                    return
                file = await doc.get_file()
                filename = doc.file_name or f"image_{int(time.time())}.jpg"
            else:
                return
            break
        except (TimedOut, NetworkError) as e:
            logger.warning(f"get_file attempt {attempt + 1} fail: {e}")
            if attempt == 2:
                await safe_reply(
                    message,
                    "⚠️ Image receive karne mein dikkat.\nThodi der baad dobara try karein."
                )
                return
            await asyncio.sleep(5)

    img_info = {
        "file_path": file.file_path,
        "filename": filename,
        "message": message,
        "user_id": user_id,
    }

    if media_group_id:
        if media_group_id not in pending_groups:
            pending_groups[media_group_id] = {
                "images": [],
                "timer": None,
                "user_id": user_id,
                "first_message": message,
                "expiry_key": None,
                "status_msg": None,
            }

        pending_groups[media_group_id]["images"].append(img_info)

        if pending_groups[media_group_id]["timer"] is None:
            async def ask_expiry_after_delay(mgid):
                await asyncio.sleep(2)
                group = pending_groups.get(mgid)
                if not group:
                    return
                count = len(group["images"])
                keyboard = get_expiry_keyboard(f"bulk_{mgid}")
                status = await safe_reply(
                    group["first_message"],
                    f"✅ *{count} images* mil gayin!\n\n⏰ *Expiry time choose karo:*",
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
                group["status_msg"] = status

            task = asyncio.create_task(ask_expiry_after_delay(media_group_id))
            pending_groups[media_group_id]["timer"] = task

    else:
        context.user_data["single_image"] = img_info

        keyboard = get_expiry_keyboard("single")
        await safe_reply(
            message,
            "✅ *Image mil gayi!*\n\n⏰ *Link kitne waqt tak active rahe?*",
            parse_mode="Markdown",
            reply_markup=keyboard
        )


# =============================================
# EXPIRY CALLBACK
# =============================================
async def expiry_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    for attempt in range(3):
        try:
            await query.answer()
            break
        except (TimedOut, NetworkError):
            if attempt < 2:
                await asyncio.sleep(3)

    data = query.data

    if data.startswith("single_"):
        expiry_key = data.replace("single_", "")
        expiry_label, expiry_val = EXPIRY_OPTIONS.get(expiry_key, ("Permanent", 0))

        img_info = context.user_data.get("single_image")
        if not img_info:
            await safe_edit(query.message, "❌ Image nahi mili! Dobara bhejein.")
            return

        status_msg = query.message
        await safe_edit(status_msg, f"⏳ Upload ho rahi hai... ({expiry_label})")

        await process_bulk_upload(
            images=[img_info],
            expiry_val=expiry_val,
            expiry_label=expiry_label,
            status_msg=status_msg,
            original_message=img_info["message"],
        )

        context.user_data.pop("single_image", None)

    elif data.startswith("bulk_"):
        parts = data.split("_")
        expiry_key = parts[-1]
        media_group_id = "_".join(parts[1:-1])

        expiry_label, expiry_val = EXPIRY_OPTIONS.get(expiry_key, ("Permanent", 0))

        group = pending_groups.get(media_group_id)
        if not group:
            await safe_edit(query.message, "❌ Images nahi mili! Dobara bhejein.")
            return

        images = group["images"]
        first_message = group["first_message"]

        status_msg = query.message
        await safe_edit(
            status_msg,
            f"⏳ {len(images)} images upload ho rahi hain... ({expiry_label})"
        )

        await process_bulk_upload(
            images=images,
            expiry_val=expiry_val,
            expiry_label=expiry_label,
            status_msg=status_msg,
            original_message=first_message,
        )

        pending_groups.pop(media_group_id, None)


# =============================================
# NON-IMAGE MESSAGE HANDLER
# =============================================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply(
        update.message,
        "📸 Mujhe sirf *image* bhejein!\n\n"
        "Ek ya zyada images ek saath bhej sakte hain.\n"
        "/help likhein madad ke liye.",
        parse_mode="Markdown"
    )


# =============================================
# MAIN FUNCTION
# =============================================
async def main():
    # ✅ Health server — Render Web Service ke liye zaroori
    thread = threading.Thread(target=run_health_server, daemon=True)
    thread.start()

    print("🤖 Bot start ho raha hai...")

    request = HTTPXRequest(
        connect_timeout=60.0,
        read_timeout=60.0,
        write_timeout=60.0,
        pool_timeout=60.0,
    )

    app = Application.builder().token(BOT_TOKEN).request(request).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_image))
    app.add_handler(CallbackQueryHandler(expiry_callback, pattern="^(single|bulk)_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)

    print("✅ Bot ready hai! Messages ka intezaar hai...")

    max_retries = 5
    for attempt in range(1, max_retries + 1):
        try:
            print(f"🔄 Connection attempt {attempt}/{max_retries}...")
            async with app:
                await app.start()
                await app.updater.start_polling(
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=True,
                )
                print("✅ Polling shuru ho gayi!")
                await asyncio.Event().wait()
            break
        except Exception as e:
            print(f"❌ Attempt {attempt} fail: {e}")
            if attempt < max_retries:
                wait_time = attempt * 5
                print(f"⏳ {wait_time} seconds mein dobara try karunga...")
                await asyncio.sleep(wait_time)
            else:
                print("❌ Saare attempts fail! Bot band ho raha hai.")
                raise


if __name__ == "__main__":
    asyncio.run(main())

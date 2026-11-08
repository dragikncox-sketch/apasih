import os
import json
import asyncio
import logging
import sqlite3
import hashlib
from datetime import datetime, timezone
from html import escape

import httpx
from dotenv import load_dotenv


# =========================================================
# CONFIG
# =========================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")

API_URL = os.getenv(
    "API_URL",
    "https://augestel.com/api/v1/iprn/messages"
)

API_KEY = os.getenv("API_KEY")
AUTH_PREFIX = os.getenv("AUTH_PREFIX", "Bearer")

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "120"))
PER_PAGE = int(os.getenv("PER_PAGE", "100"))
MESSAGE_TYPE = os.getenv("MESSAGE_TYPE", "a2p")

DB_FILE = "forwarded_messages.db"


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger("forwarder")


# =========================================================
# VALIDASI CONFIG
# =========================================================

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN belum diisi di file .env")

if not CHANNEL_ID:
    raise RuntimeError("CHANNEL_ID belum diisi di file .env")

if not API_KEY:
    raise RuntimeError("API_KEY belum diisi di file .env")


# =========================================================
# DATABASE
# =========================================================

def init_database():
    """Membuat database untuk mencegah pesan dikirim dua kali."""

    conn = sqlite3.connect(DB_FILE)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS forwarded_messages (
            message_id TEXT PRIMARY KEY,
            forwarded_at TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()


def is_forwarded(message_id: str) -> bool:
    """Cek apakah pesan sudah pernah dikirim."""

    conn = sqlite3.connect(DB_FILE)

    cursor = conn.execute(
        "SELECT 1 FROM forwarded_messages WHERE message_id = ?",
        (message_id,)
    )

    result = cursor.fetchone()

    conn.close()

    return result is not None


def mark_forwarded(message_id: str):
    """Tandai pesan sebagai sudah berhasil dikirim."""

    conn = sqlite3.connect(DB_FILE)

    conn.execute(
        """
        INSERT OR IGNORE INTO forwarded_messages
        (message_id, forwarded_at)
        VALUES (?, ?)
        """,
        (
            message_id,
            datetime.now(timezone.utc).isoformat()
        )
    )

    conn.commit()
    conn.close()


# =========================================================
# API WEBSITE
# =========================================================

def get_headers():
    return {
        "Authorization": f"{AUTH_PREFIX} {API_KEY}",
        "Accept": "application/json",
    }


async def get_messages(client: httpx.AsyncClient):
    """
    Mengambil pesan dari API website.
    """

    params = {
        "per_page": PER_PAGE,
        "page": 1,
    }

    if MESSAGE_TYPE:
        params["type"] = MESSAGE_TYPE

    try:
        response = await client.get(
            API_URL,
            headers=get_headers(),
            params=params,
        )

        # Jika API melakukan rate limiting
        if response.status_code == 429:

            retry_after = response.headers.get(
                "Retry-After",
                "60"
            )

            try:
                retry_after = int(float(retry_after))
            except ValueError:
                retry_after = 60

            logger.warning(
                "API sedang membatasi request. "
                "Menunggu %s detik.",
                retry_after
            )

            await asyncio.sleep(retry_after)

            return []

        response.raise_for_status()

        data = response.json()

        logger.info(
            "Berhasil mengambil data dari API."
        )

        # Struktur: [...]
        if isinstance(data, list):
            return data

        # Struktur JSON object
        if isinstance(data, dict):

            # {"data": [...]}
            if isinstance(data.get("data"), list):
                return data["data"]

            # {"messages": [...]}
            if isinstance(data.get("messages"), list):
                return data["messages"]

            # {"data": {"data": [...]}}
            nested_data = data.get("data")

            if isinstance(nested_data, dict):

                if isinstance(
                    nested_data.get("data"),
                    list
                ):
                    return nested_data["data"]

        logger.warning(
            "Struktur respons API tidak dikenali: %s",
            json.dumps(data)[:500]
        )

        return []

    except httpx.HTTPStatusError as e:

        logger.error(
            "HTTP Error API: %s - %s",
            e.response.status_code,
            e.response.text[:300]
        )

        return []

    except httpx.RequestError as e:

        logger.error(
            "Gagal terhubung ke API: %s",
            e
        )

        return []

    except Exception:

        logger.exception(
            "Terjadi kesalahan saat mengambil pesan."
        )

        return []


# =========================================================
# HELPER DATA
# =========================================================

def get_value(message: dict, *keys, default="-"):
    """Mengambil nilai dari beberapa kemungkinan nama field."""

    for key in keys:

        value = message.get(key)

        if value is not None and value != "":
            return str(value)

    return default


def get_message_id(message: dict) -> str:
    """Menghasilkan ID unik untuk mencegah duplikasi."""

    message_id = get_value(
        message,
        "id",
        "message_id",
        "uuid",
        default=""
    )

    if message_id:
        return message_id

    # Jika API tidak memiliki ID unik,
    # gunakan hash dari seluruh isi data.
    raw = json.dumps(
        message,
        sort_keys=True,
        ensure_ascii=False
    )

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()


# =========================================================
# FORMAT PESAN
# =========================================================

def format_message(message: dict) -> str:
    """
    Format pesan untuk Telegram.

    Semua data dari API di-escape agar karakter seperti:
    < > &
    tidak dianggap sebagai tag HTML Telegram.
    """

    message_id = get_value(
        message,
        "id",
        "message_id",
        "uuid"
    )

    number = get_value(
        message,
        "number",
        "phone_number",
        "destination",
        "to",
        "msisdn"
    )

    sender = get_value(
        message,
        "sender",
        "from",
        "source"
    )

    created_at = get_value(
        message,
        "created_at",
        "timestamp",
        "received_at",
        "date"
    )

    content = get_value(
        message,
        "message",
        "content",
        "body",
        "text",
        "sms"
    )

    # PENTING:
    # Escape data dari API sebelum dimasukkan ke HTML Telegram.
    message_id = escape(str(message_id))
    number = escape(str(number))
    sender = escape(str(sender))
    created_at = escape(str(created_at))
    content = escape(str(content))

    return (
        "📩 <b>PESAN BARU</b>\n\n"
        f"📱 <b>Nomor:</b> {number}\n"
        f"👤 <b>Pengirim:</b> {sender}\n"
        f"🕒 <b>Waktu:</b> {created_at}\n"
        f"🆔 <b>ID:</b> {message_id}\n\n"
        f"💬 <b>Pesan:</b>\n"
        f"<code>{content}</code>"
    )


# =========================================================
# KIRIM KE TELEGRAM
# =========================================================

async def send_to_telegram(
    client: httpx.AsyncClient,
    text: str
) -> bool:

    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": CHANNEL_ID,
        "text": text,
        "parse_mode": "HTML",
    }

    try:

        response = await client.post(
            url,
            json=payload
        )

        response.raise_for_status()

        return True

    except httpx.HTTPStatusError as e:

        logger.error(
            "Telegram error: %s - %s",
            e.response.status_code,
            e.response.text[:500]
        )

        return False

    except Exception:

        logger.exception(
            "Gagal mengirim pesan ke Telegram."
        )

        return False


# =========================================================
# PROSES PESAN
# =========================================================

async def process_messages(client: httpx.AsyncClient):

    messages = await get_messages(client)

    if not messages:

        logger.info(
            "Tidak ada pesan baru dari API."
        )

        return

    logger.info(
        "Memeriksa %s pesan dari API.",
        len(messages)
    )

    # Urutkan berdasarkan waktu jika field tersedia
    messages = sorted(
        messages,
        key=lambda x: str(
            x.get("created_at", "")
        )
    )

    sent_count = 0

    for message in messages:

        if not isinstance(message, dict):
            continue

        message_id = get_message_id(message)

        # Sudah dikirim sebelumnya
        if is_forwarded(message_id):
            continue

        text = format_message(message)

        success = await send_to_telegram(
            client,
            text
        )

        # Hanya simpan ke database jika
        # Telegram benar-benar berhasil menerima pesan.
        if success:

            mark_forwarded(message_id)

            sent_count += 1

            logger.info(
                "Pesan %s berhasil dikirim.",
                message_id
            )

        # Jeda kecil
        await asyncio.sleep(0.3)

    logger.info(
        "Selesai. %s pesan baru dikirim.",
        sent_count
    )


# =========================================================
# MAIN LOOP
# =========================================================

async def main():

    init_database()

    logger.info("=" * 50)
    logger.info(
        "WEBSITE → TELEGRAM FORWARDER AKTIF"
    )
    logger.info(
        "Channel ID: %s",
        CHANNEL_ID
    )
    logger.info(
        "Interval: %s detik",
        POLL_INTERVAL
    )
    logger.info("=" * 50)

    timeout = httpx.Timeout(30.0)

    async with httpx.AsyncClient(
        timeout=timeout
    ) as client:

        while True:

            try:

                await process_messages(client)

            except Exception:

                logger.exception(
                    "Terjadi kesalahan pada proses utama."
                )

            logger.info(
                "Menunggu %s detik...",
                POLL_INTERVAL
            )

            await asyncio.sleep(
                POLL_INTERVAL
            )


if __name__ == "__main__":

    try:

        asyncio.run(main())

    except KeyboardInterrupt:

        logger.info(
            "Forwarder dihentikan."
        )
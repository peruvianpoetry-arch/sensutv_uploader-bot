import os
import re
import json
import time
import logging
from datetime import datetime
from io import BytesIO

from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

import boto3

# ---------------- LOG ----------------
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("sensutv-uploader")

# ---------------- ENV ----------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")

WASABI_ACCESS_KEY = os.environ.get("WASABI_ACCESS_KEY")
WASABI_SECRET_KEY = os.environ.get("WASABI_SECRET_KEY")
WASABI_BUCKET = os.environ.get("WASABI_BUCKET", "sensutv-media")
WASABI_REGION = os.environ.get("WASABI_REGION", "eu-central-2")
WASABI_ENDPOINT = os.environ.get("WASABI_ENDPOINT", "https://s3.eu-central-2.wasabisys.com")

# Admins: IDs separados por coma (ej: "12345,67890")
ADMIN_USER_IDS = set()
if os.environ.get("ADMIN_USER_IDS"):
    ADMIN_USER_IDS = {int(x.strip()) for x in os.environ["ADMIN_USER_IDS"].split(",") if x.strip().isdigit()}

# ---------------- S3 Client ----------------
s3 = boto3.client(
    "s3",
    region_name=WASABI_REGION,
    endpoint_url=WASABI_ENDPOINT,
    aws_access_key_id=WASABI_ACCESS_KEY,
    aws_secret_access_key=WASABI_SECRET_KEY,
)

# ---------------- SIMPLE STATE (RAM) ----------------
# En producción luego lo persistimos (manifest/profile en Wasabi).
SESSION = {}  # user_id -> dict

MANIFEST_KEY = "data/manifest.json"  # la webapp leerá esto

# ---------------- HELPERS ----------------
def is_admin(user_id: int) -> bool:
    return (not ADMIN_USER_IDS) or (user_id in ADMIN_USER_IDS)

def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")

def today_path() -> str:
    now = datetime.utcnow()
    return now.strftime("%Y/%m/%d")

def s3_put_bytes(key: str, data: bytes, content_type: str):
    s3.put_object(
        Bucket=WASABI_BUCKET,
        Key=key,
        Body=data,
        ContentType=content_type,
    )

def s3_get_json_or_default(key: str, default):
    try:
        obj = s3.get_object(Bucket=WASABI_BUCKET, Key=key)
        raw = obj["Body"].read()
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return default

def s3_put_json(key: str, payload):
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    s3_put_bytes(key, data, "application/json")

def ensure_manifest():
    m = s3_get_json_or_default(MANIFEST_KEY, {"models": {}, "items": []})
    if "models" not in m: m["models"] = {}
    if "items" not in m: m["items"] = []
    return m

def ensure_model_profile(model_slug: str, profile: dict):
    key = f"models/{model_slug}/profile.json"
    existing = s3_get_json_or_default(key, None)
    if existing is None:
        s3_put_json(key, profile)

# ---------------- BOT COMMANDS ----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ No autorizado.")
        return

    await update.message.reply_text(
        "✅ SensuTV Uploader listo.\n\n"
        "1) /newmodel para registrar modelo\n"
        "2) /setmodel <nombre> para seleccionar modelo\n"
        "3) Envíame foto o video y lo subo a Wasabi\n\n"
        "Tip: /whoami para ver tu ID"
    )

async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await update.message.reply_text(f"👤 Tu ID: {u.id}\nUsername: @{u.username}")

async def cmd_newmodel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ No autorizado.")
        return
    SESSION[update.effective_user.id] = {"step": "model_name"}
    await update.message.reply_text("🧩 Nombre de la modelo? (ej: Aurora)")

async def cmd_setmodel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ No autorizado.")
        return
    if not context.args:
        await update.message.reply_text("Usa: /setmodel Aurora")
        return
    name = " ".join(context.args).strip()
    model_slug = slugify(name)
    SESSION.setdefault(update.effective_user.id, {})
    SESSION[update.effective_user.id]["model_slug"] = model_slug
    SESSION[update.effective_user.id]["model_name"] = name
    await update.message.reply_text(f"✅ Modelo activo: {name} ({model_slug})\nAhora envíame un video o foto.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    uid = update.effective_user.id
    st = SESSION.get(uid, {})
    step = st.get("step")

    if step == "model_name":
        name = update.message.text.strip()
        st["model_name"] = name
        st["model_slug"] = slugify(name)
        st["step"] = "model_age"
        SESSION[uid] = st
        await update.message.reply_text("🎂 Edad? (solo número, ej 23)")
        return

    if step == "model_age":
        age_txt = update.message.text.strip()
        if not age_txt.isdigit():
            await update.message.reply_text("Pon solo número (ej 23).")
            return
        st["age"] = int(age_txt)
        st["step"] = "model_country"
        await update.message.reply_text("🌍 País? (ej: Brasil)")
        return

    if step == "model_country":
        st["country"] = update.message.text.strip()
        st["step"] = "model_tags"
        await update.message.reply_text("🏷️ Categorías (separadas por coma). Ej: latina, milf, cosplay")
        return

    if step == "model_tags":
        tags = [x.strip() for x in update.message.text.split(",") if x.strip()]
        st["tags"] = tags
        st["step"] = None

        # Guardar profile
        model_slug = st["model_slug"]
        profile = {
            "name": st["model_name"],
            "slug": model_slug,
            "age": st.get("age"),
            "country": st.get("country"),
            "tags": st.get("tags", []),
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }
        ensure_model_profile(model_slug, profile)

        # Registrar en manifest
        m = ensure_manifest()
        m["models"][model_slug] = profile
        s3_put_json(MANIFEST_KEY, m)

        await update.message.reply_text(
            f"✅ Modelo creada y guardada:\n"
            f"- {profile['name']} ({profile['slug']})\n"
            f"- {profile['country']} | {profile['age']}\n"
            f"- tags: {', '.join(profile['tags'])}\n\n"
            f"Ahora: /setmodel {profile['name']} y envíame media."
        )
        SESSION[uid] = st
        return

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    uid = update.effective_user.id
    st = SESSION.get(uid, {})
    model_slug = st.get("model_slug")
    model_name = st.get("model_name")

    if not model_slug:
        await update.message.reply_text("⚠️ Primero selecciona modelo: /setmodel Aurora\nO crea: /newmodel")
        return

    # Determinar archivo
    file = None
    filename = None
    content_type = "application/octet-stream"
    thumb_file = None
    thumb_ct = None

    if update.message.photo:
        # mejor calidad al final
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        filename = f"photo_{int(time.time())}.jpg"
        content_type = "image/jpeg"

    elif update.message.video:
        v = update.message.video
        file = await context.bot.get_file(v.file_id)
        filename = v.file_name or f"video_{int(time.time())}.mp4"
        content_type = v.mime_type or "video/mp4"

        # Thumbnail (Telegram a veces lo trae)
        if v.thumbnail:
            thumb_file = await context.bot.get_file(v.thumbnail.file_id)
            thumb_ct = "image/jpeg"

    else:
        await update.message.reply_text("Envíame una foto o video.")
        return

    # Descargar bytes
    b = BytesIO()
    await file.download_to_memory(out=b)
    data = b.getvalue()

    date_path = today_path()
    key_media = f"models/{model_slug}/media/{date_path}/{filename}"

    # Subir media
    s3_put_bytes(key_media, data, content_type)

    # Subir thumb si existe (para videos)
    key_thumb = None
    if thumb_file:
        tb = BytesIO()
        await thumb_file.download_to_memory(out=tb)
        tdata = tb.getvalue()
        key_thumb = f"models/{model_slug}/thumbs/{date_path}/{slugify(filename)}.jpg"
        s3_put_bytes(key_thumb, tdata, "image/jpeg")

    # Actualizar manifest (un item nuevo)
    m = ensure_manifest()
    item = {
        "id": f"{model_slug}-{int(time.time())}",
        "model": model_slug,
        "model_name": model_name,
        "type": "video" if "video" in content_type else "photo",
        "key": key_media,
        "thumb_key": key_thumb,
        "content_type": content_type,
        "created_at": datetime.utcnow().isoformat() + "Z",
    }
    m["items"].insert(0, item)  # lo más nuevo arriba
    s3_put_json(MANIFEST_KEY, m)

    await update.message.reply_text(
        "✅ Subido a Wasabi!\n"
        f"Modelo: {model_name}\n"
        f"Archivo: {filename}\n"
        f"Ruta: {key_media}\n"
        + (f"Thumb: {key_thumb}\n" if key_thumb else "")
        + "\n👉 La webapp lo podrá detectar vía manifest."
    )

# ---------------- FLASK KEEPALIVE ----------------
app = Flask(__name__)

@app.get("/")
def home():
    return "OK - SensuTV Uploader Bot"

def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Falta TELEGRAM_TOKEN en variables de entorno.")
    if not WASABI_ACCESS_KEY or not WASABI_SECRET_KEY:
        raise RuntimeError("Faltan WASABI_ACCESS_KEY / WASABI_SECRET_KEY.")

    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("whoami", cmd_whoami))
    application.add_handler(CommandHandler("newmodel", cmd_newmodel))
    application.add_handler(CommandHandler("setmodel", cmd_setmodel))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO, handle_media))

    # polling
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    # Render necesita puerto abierto (keep-alive)
    port = int(os.environ.get("PORT", "10000"))
    from threading import Thread
    Thread(target=lambda: app.run(host="0.0.0.0", port=port), daemon=True).start()
    main()

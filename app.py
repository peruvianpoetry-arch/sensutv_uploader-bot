import os
import json
import time
import logging
import threading
from datetime import datetime
from typing import Dict, Any

from flask import Flask, jsonify, render_template_string, request

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# =========================
# LOGGING
# =========================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
logger = logging.getLogger("sensutv-uploader")

# =========================
# ENV VARS
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")  # obligatorio
PORT = int(os.getenv("PORT", "10000"))
BOT_PAY_LINK = os.getenv("BOT_PAY_LINK", "").strip()

WASABI_BUCKET = os.getenv("WASABI_BUCKET", "sensutv-media")
WASABI_REGION = os.getenv("WASABI_REGION", "eu-central-2")

DEFAULT_DATA_DIR = os.getenv("DATA_DIR", "/var/data")
DATA_DIR = DEFAULT_DATA_DIR

def ensure_data_dir():
    global DATA_DIR
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        testfile = os.path.join(DATA_DIR, ".write_test")
        with open(testfile, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(testfile)
        logger.info("✅ DATA_DIR OK: %s", DATA_DIR)
    except Exception as e:
        logger.warning("⚠️ No se pudo usar DATA_DIR=%s (%s). fallback /tmp/data", DATA_DIR, e)
        DATA_DIR = "/tmp/data"
        os.makedirs(DATA_DIR, exist_ok=True)
        logger.info("✅ DATA_DIR fallback activo: %s", DATA_DIR)

ensure_data_dir()

MODELS_FILE = os.path.join(DATA_DIR, "models.json")
UPLOADS_FILE = os.path.join(DATA_DIR, "uploads.json")

# =========================
# HELPERS JSON
# =========================
def _load_json(path: str, default: Any):
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.exception("Error leyendo %s: %s", path, e)
        return default

def _save_json(path: str, data: Any):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def load_models() -> Dict[str, Any]:
    return _load_json(MODELS_FILE, {})

def save_models(models: Dict[str, Any]):
    _save_json(MODELS_FILE, models)

def load_uploads() -> Dict[str, Any]:
    return _load_json(UPLOADS_FILE, {"items": []})

def save_uploads(data: Dict[str, Any]):
    _save_json(UPLOADS_FILE, data)

def slugify(s: str) -> str:
    s = s.strip().lower()
    out = []
    for ch in s:
        if ch.isalnum():
            out.append(ch)
        elif ch in [" ", ".", "/", "\\", "|", ":", ";", ",", "+", "&"]:
            out.append("-")
        elif ch in ["_", "-"]:
            out.append(ch)
    res = "".join(out)
    while "--" in res:
        res = res.replace("--", "-")
    return res.strip("-")

def now_yyyymmdd() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")

# =========================
# FLASK (opcional, health + APIs)
# =========================
app = Flask(__name__)

@app.get("/healthz")
def healthz():
    return "ok", 200

@app.get("/api/models")
def api_models():
    return jsonify(load_models())

@app.get("/api/uploads")
def api_uploads():
    return jsonify(load_uploads())

def run_flask():
    logger.info("Starting Flask on port %s", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False)

# =========================
# TELEGRAM BOT
# =========================
S_MODEL_NAME, S_COUNTRY, S_AGE, S_TAGS, S_TYPE, S_CATEGORY = range(6)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "✅ *SensuTV Uploader Bot activo*\n\n"
        "Comandos:\n"
        "• /register → registrar modelo\n"
        "• /models → listar modelos\n"
        "• /plan → generar ruta para Wasabi\n"
        "• /last → últimas rutas\n\n"
        f"📦 Bucket: `{WASABI_BUCKET}`\n"
        f"🌍 Region: `{WASABI_REGION}`\n"
        f"💾 DATA_DIR: `{DATA_DIR}`\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def cmd_models(update: Update, context: ContextTypes.DEFAULT_TYPE):
    models = load_models()
    if not models:
        await update.message.reply_text("Aún no hay modelos registradas. Usa /register")
        return
    lines = ["📋 *Modelos registradas:*"]
    for k, v in models.items():
        tags = ", ".join(v.get("tags", [])) if v.get("tags") else "-"
        lines.append(f"• *{v.get('name','')}* ({v.get('country','')}) — edad: {v.get('age','?')} — tags: {tags} — id: `{k}`")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def cmd_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uploads = load_uploads().get("items", [])
    if not uploads:
        await update.message.reply_text("No hay registros aún. Usa /plan para generar rutas.")
        return
    last = list(reversed(uploads))[:10]
    lines = ["🕒 *Últimas rutas generadas:*"]
    for it in last:
        lines.append(f"• {it.get('date','')} — *{it.get('model_name','')}* — `{it.get('path','')}`")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

# ---- REGISTER ----
async def register_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Nombre de la modelo (ej: Aurora):")
    return S_MODEL_NAME

async def register_model_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["model_name"] = update.message.text.strip()
    await update.message.reply_text("País (ej: Brasil, Perú, Alemania):")
    return S_COUNTRY

async def register_country(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["country"] = update.message.text.strip()
    await update.message.reply_text("Edad (solo número, ej: 23):")
    return S_AGE

async def register_age(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    age = "".join([c for c in txt if c.isdigit()])
    context.user_data["age"] = age if age else "?"
    await update.message.reply_text("Tags separadas por coma (ej: latina, milf, cosplay):")
    return S_TAGS

async def register_tags(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    tags = [slugify(x) for x in raw.split(",") if x.strip()]

    name = context.user_data.get("model_name", "").strip()
    country = context.user_data.get("country", "").strip()
    age = context.user_data.get("age", "?")

    model_id = slugify(name) or f"model-{int(time.time())}"
    models = load_models()
    models[model_id] = {
        "id": model_id,
        "name": name,
        "country": country,
        "age": age,
        "tags": tags,
        "created_at": datetime.utcnow().isoformat() + "Z",
    }
    save_models(models)

    await update.message.reply_text(
        f"✅ Registrada: *{name}*\nID: `{model_id}`\nPaís: {country}\nEdad: {age}\nTags: {', '.join(tags) if tags else '-'}",
        parse_mode=ParseMode.MARKDOWN,
    )
    context.user_data.clear()
    return ConversationHandler.END

# ---- PLAN ----
async def plan_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    models = load_models()
    if not models:
        await update.message.reply_text("Primero registra una modelo con /register")
        return ConversationHandler.END

    lines = ["Elige modelo (escribe el *ID*):"]
    for k, v in models.items():
        lines.append(f"• `{k}` = {v.get('name','')} ({v.get('country','')})")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    return S_MODEL_NAME

async def plan_pick_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    model_id = slugify(update.message.text.strip())
    models = load_models()
    if model_id not in models:
        await update.message.reply_text("❌ ID no válido. Copia/pega el ID exacto.")
        return S_MODEL_NAME
    context.user_data["plan_model_id"] = model_id
    await update.message.reply_text("Tipo de archivo: escribe `video` o `foto`")
    return S_TYPE

async def plan_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = slugify(update.message.text.strip())
    if t not in ["video", "foto"]:
        await update.message.reply_text("Escribe solo: `video` o `foto`")
        return S_TYPE
    context.user_data["plan_type"] = t
    await update.message.reply_text("Categoría (ej: free, premium, teaser, cosplay):")
    return S_CATEGORY

async def plan_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat = slugify(update.message.text.strip()) or "general"
    model_id = context.user_data["plan_model_id"]
    t = context.user_data["plan_type"]

    models = load_models()
    m = models[model_id]
    date = now_yyyymmdd()

    country_slug = slugify(m.get("country", "unknown")) or "unknown"
    path = f"{country_slug}/{model_id}/{t}/{cat}/{date}/"

    uploads = load_uploads()
    uploads["items"].append({
        "bucket": WASABI_BUCKET,
        "region": WASABI_REGION,
        "model_id": model_id,
        "model_name": m.get("name", ""),
        "country": m.get("country", ""),
        "type": t,
        "category": cat,
        "date": date,
        "title": f"{m.get('name','')} • {t} • {cat}",
        "path": path,
        "created_at": datetime.utcnow().isoformat() + "Z",
    })
    save_uploads(uploads)

    msg = (
        "✅ *Ruta generada*\n\n"
        f"Modelo: *{m.get('name','')}*\n"
        f"Tipo: *{t}*\n"
        f"Categoría: *{cat}*\n"
        f"Fecha: *{date}*\n\n"
        f"📦 Bucket: `{WASABI_BUCKET}`\n"
        f"🧭 Ruta: `{path}`\n\n"
        "👉 Sube tus archivos a esa carpeta en Wasabi.\n"
        "Luego la web (sensutv-webapp) puede leer el registro y mostrar “últimas subidas”."
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Cancelado.")
    return ConversationHandler.END

def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Falta TELEGRAM_TOKEN en Render (Environment).")

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("models", cmd_models))
    application.add_handler(CommandHandler("last", cmd_last))

    register_conv = ConversationHandler(
        entry_points=[CommandHandler("register", register_start)],
        states={
            S_MODEL_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_model_name)],
            S_COUNTRY: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_country)],
            S_AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_age)],
            S_TAGS: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_tags)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    plan_conv = ConversationHandler(
        entry_points=[CommandHandler("plan", plan_start)],
        states={
            S_MODEL_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, plan_pick_model)],
            S_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, plan_type)],
            S_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, plan_category)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    application.add_handler(register_conv)
    application.add_handler(plan_conv)

    logger.info("Telegram bot starting polling...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

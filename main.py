"""
Vigila los retweets de una cuenta de X (Twitter) y los reenvía a un
tema (topic) específico de un grupo de Telegram.

Envía solo el texto de la noticia (sin links de artículos externos) y,
si el tweet original tiene foto(s) o video, adjunta la imagen.

Variables de entorno necesarias (se configuran como "Secrets" en GitHub Actions):
    X_BEARER_TOKEN        -> Bearer Token de tu app en developer.x.com
    X_USER_ID             -> ID numérico de la cuenta que quieres vigilar
    TELEGRAM_BOT_TOKEN    -> Token que te dio @BotFather
    TELEGRAM_CHAT_ID      -> ID del grupo (negativo, ej: -1001234567890)
    TELEGRAM_THREAD_ID    -> ID del tema "Noticias" dentro del grupo

Guarda el último tweet visto en state.json para no reenviar lo mismo dos veces.
"""

import os
import re
import sys
import json
import time
import requests
from deep_translator import GoogleTranslator, MyMemoryTranslator

STATE_FILE = "state.json"

X_BEARER_TOKEN = os.environ["X_BEARER_TOKEN"]
X_USER_ID = os.environ["X_USER_ID"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TELEGRAM_THREAD_ID = os.environ.get("TELEGRAM_THREAD_ID")  # opcional

X_TIMELINE_URL = f"https://api.x.com/2/users/{X_USER_ID}/tweets"
X_TWEETS_LOOKUP_URL = "https://api.x.com/2/tweets"
TELEGRAM_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

URL_RE = re.compile(r"https?://t\.co/\S+")
X_HEADERS = {"Authorization": f"Bearer {X_BEARER_TOKEN}"}


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"since_id": None}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def fetch_timeline(since_id):
    """Trae los tweets/retweets recientes del usuario vigilado."""
    params = {
        "exclude": "replies",
        "max_results": 20,
        "tweet.fields": "created_at,referenced_tweets",
    }
    if since_id:
        params["since_id"] = since_id

    resp = requests.get(X_TIMELINE_URL, headers=X_HEADERS, params=params, timeout=30)
    if resp.status_code != 200:
        print(f"Error consultando el timeline: {resp.status_code} {resp.text}")
        sys.exit(1)
    return resp.json()


def fetch_originals(tweet_ids):
    """Trae texto + media de los tweets originales (los que fueron retuiteados)."""
    if not tweet_ids:
        return {}

    params = {
        "ids": ",".join(tweet_ids),
        "tweet.fields": "text,attachments,note_tweet",
        "expansions": "attachments.media_keys",
        "media.fields": "url,preview_image_url,type",
    }
    resp = requests.get(X_TWEETS_LOOKUP_URL, headers=X_HEADERS, params=params, timeout=30)
    if resp.status_code != 200:
        print(f"Error consultando tweets originales: {resp.status_code} {resp.text}")
        return {}

    payload = resp.json()
    tweets = {t["id"]: t for t in payload.get("data", [])}
    media_by_key = {m["media_key"]: m for m in payload.get("includes", {}).get("media", [])}

    result = {}
    for tid, tweet in tweets.items():
        # Si el tweet es "largo" (formato extendido de X), el texto completo
        # viene en note_tweet.text; el campo "text" normal viene recortado.
        full_text = tweet.get("note_tweet", {}).get("text") or tweet.get("text", "")
        text = URL_RE.sub("", full_text).strip()
        text = translate_to_spanish(text)
        time.sleep(1.5)  # pausa entre traducciones para no saturar a Google Translate

        photos = []
        for key in tweet.get("attachments", {}).get("media_keys", []):
            media = media_by_key.get(key)
            if not media:
                continue
            if media.get("type") == "photo" and media.get("url"):
                photos.append(media["url"])
            elif media.get("preview_image_url"):
                photos.append(media["preview_image_url"])

        result[tid] = {"text": text, "photos": photos}

    return result


BROKEN_TRANSLATION_MARKERS = [
    "error 500",
    "server error",
    "please try again later",
    "that's an error",
    "that's all we know",
    "bad request",
    "service unavailable",
    "invalid source language",
    "invalid target language",
    "is an invalid",
    "using 2 letter iso",
    "no support for the provided language",
]


def looks_broken(original, translated):
    """Detecta si Google Translate devolvió una página de error en vez de traducir."""
    if not translated:
        return True
    lowered = translated.lower()
    if any(marker in lowered for marker in BROKEN_TRANSLATION_MARKERS):
        return True
    # Si la traducción quedó absurdamente más corta o larga que el original, sospechamos.
    if len(original) > 20 and len(translated) < len(original) * 0.2:
        return True
    return False


def split_into_chunks(text, max_len=450):
    """Parte el texto en trozos manejables (por oración) para traducir sin fallos."""
    if len(text) <= max_len:
        return [text]

    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks = []
    current = ""
    for sentence in sentences:
        if len(current) + len(sentence) + 1 <= max_len:
            current = f"{current} {sentence}".strip()
        else:
            if current:
                chunks.append(current)
            # Si una sola "oración" ya es más larga que max_len, la partimos a la fuerza.
            while len(sentence) > max_len:
                chunks.append(sentence[:max_len])
                sentence = sentence[max_len:]
            current = sentence
    if current:
        chunks.append(current)
    return chunks


def try_google(chunk):
    return GoogleTranslator(source="auto", target="es").translate(chunk)


def try_mymemory(chunk):
    # MyMemory no soporta "auto" como idioma de origen (a diferencia de Google);
    # como las cuentas que monitoreamos tuitean en inglés, lo fijamos directo.
    return MyMemoryTranslator(source="en", target="es-ES").translate(chunk)


TRANSLATOR_ENGINES = [try_google, try_mymemory]


def translate_to_spanish(text, attempts=4):
    if not text:
        return text

    chunks = split_into_chunks(text)
    translated_chunks = []

    for chunk in chunks:
        translated = None
        for attempt in range(attempts):
            for engine in TRANSLATOR_ENGINES:
                try:
                    candidate = engine(chunk)
                    if not looks_broken(chunk, candidate):
                        translated = candidate
                        break
                    print(f"Traducción sospechosa con {engine.__name__}, probando otro motor...")
                except Exception as e:
                    print(f"Fallo con {engine.__name__} (intento {attempt + 1}): {e}")
            if translated:
                break
            # Espera cada vez más larga entre reintentos: 3s, 6s, 12s, 24s, 48s...
            wait = min(3 * (2 ** attempt), 60)
            print(f"Reintentando traducción en {wait}s...")
            time.sleep(wait)

        translated_chunks.append(translated if translated else chunk)
        time.sleep(0.7)  # pausa breve entre fragmentos del mismo texto

    return " ".join(translated_chunks)


def extract_retweets(timeline_data):
    """Devuelve [{tweet_id, original_id}] en orden cronológico + el newest_id."""
    tweets = timeline_data.get("data", [])
    newest_id = timeline_data.get("meta", {}).get("newest_id")
    if not tweets:
        return [], newest_id

    retweets = []
    for tweet in tweets:
        for ref in tweet.get("referenced_tweets", []):
            if ref["type"] == "retweeted":
                retweets.append({"tweet_id": tweet["id"], "original_id": ref["id"]})

    retweets.reverse()  # la API devuelve lo más nuevo primero
    return retweets, newest_id


def send_text(text):
    resp = requests.post(
        f"{TELEGRAM_BASE}/sendMessage",
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "message_thread_id": TELEGRAM_THREAD_ID,
            "text": text,
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"Error enviando texto a Telegram: {resp.status_code} {resp.text}")


def send_single_photo(photo_url, caption):
    resp = requests.post(
        f"{TELEGRAM_BASE}/sendPhoto",
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "message_thread_id": TELEGRAM_THREAD_ID,
            "photo": photo_url,
            "caption": caption,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"Error enviando foto a Telegram: {resp.status_code} {resp.text}")


def send_media_group(photo_urls, caption):
    media = []
    for i, url in enumerate(photo_urls[:10]):  # Telegram permite máx 10
        item = {"type": "photo", "media": url}
        if i == 0:
            item["caption"] = caption
        media.append(item)

    resp = requests.post(
        f"{TELEGRAM_BASE}/sendMediaGroup",
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "message_thread_id": TELEGRAM_THREAD_ID,
            "media": json.dumps(media),
        },
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"Error enviando álbum a Telegram: {resp.status_code} {resp.text}")


TELEGRAM_CAPTION_LIMIT = 1024


def send_retweet(text, photos):
    text = text if text else "📰"

    if not photos:
        send_text(text)
        return

    # Si el texto no cabe como "caption" de una foto, se manda la foto
    # (sin texto) y el texto completo en un mensaje aparte, justo después.
    if len(text) > TELEGRAM_CAPTION_LIMIT:
        if len(photos) == 1:
            send_single_photo(photos[0], "")
        else:
            send_media_group(photos, "")
        send_text(text)
    else:
        if len(photos) == 1:
            send_single_photo(photos[0], text)
        else:
            send_media_group(photos, text)


def main():
    state = load_state()
    timeline_data = fetch_timeline(state.get("since_id"))
    retweets, newest_id = extract_retweets(timeline_data)

    print(f"Retweets nuevos encontrados: {len(retweets)}")

    if retweets:
        original_ids = list({rt["original_id"] for rt in retweets})
        originals = fetch_originals(original_ids)

        for rt in retweets:
            info = originals.get(rt["original_id"])
            if not info:
                continue
            send_retweet(info["text"], info["photos"])
            time.sleep(1)  # pequeño respiro entre mensajes

    if newest_id:
        state["since_id"] = newest_id
        save_state(state)


if __name__ == "__main__":
    main()

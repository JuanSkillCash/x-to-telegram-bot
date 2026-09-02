"""
Vigila los retweets de una cuenta de X (Twitter) y los reenvía a un
tema (topic) específico de un grupo de Telegram.

Envía solo el texto de la noticia (sin links de artículos externos) y,
si el tweet tiene foto(s), las adjunta como imagen real.

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

STATE_FILE = "state.json"

X_BEARER_TOKEN = os.environ["X_BEARER_TOKEN"]
X_USER_ID = os.environ["X_USER_ID"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TELEGRAM_THREAD_ID = os.environ.get("TELEGRAM_THREAD_ID")  # opcional

X_API_URL = f"https://api.x.com/2/users/{X_USER_ID}/tweets"
TELEGRAM_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

URL_RE = re.compile(r"https?://t\.co/\S+")


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"since_id": None}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def fetch_new_tweets(since_id):
    params = {
        "exclude": "replies",
        "max_results": 20,
        "tweet.fields": "created_at,referenced_tweets,attachments",
        "expansions": "referenced_tweets.id,referenced_tweets.id.author_id,attachments.media_keys",
        "user.fields": "username",
        "media.fields": "url,preview_image_url,type",
    }
    if since_id:
        params["since_id"] = since_id

    headers = {"Authorization": f"Bearer {X_BEARER_TOKEN}"}
    resp = requests.get(X_API_URL, headers=headers, params=params, timeout=30)

    if resp.status_code != 200:
        print(f"Error consultando la API de X: {resp.status_code} {resp.text}")
        sys.exit(1)

    return resp.json()


def clean_text(text):
    """Quita los links t.co (artículos externos, etc.) del texto del tweet."""
    text = URL_RE.sub("", text).strip()
    return text


def extract_retweets(data):
    """Devuelve una lista de dicts {tweet_id, text, photos} listos para enviar."""
    tweets = data.get("data", [])
    if not tweets:
        return [], data.get("meta", {}).get("newest_id")

    included_tweets = {t["id"]: t for t in data.get("includes", {}).get("tweets", [])}
    included_media = {m["media_key"]: m for m in data.get("includes", {}).get("media", [])}

    retweets = []
    for tweet in tweets:
        refs = tweet.get("referenced_tweets", [])
        for ref in refs:
            if ref["type"] != "retweeted":
                continue
            original = included_tweets.get(ref["id"])
            if not original:
                continue

            text = clean_text(original.get("text", ""))

            photos = []
            media_keys = original.get("attachments", {}).get("media_keys", [])
            for key in media_keys:
                media = included_media.get(key)
                if not media:
                    continue
                if media.get("type") == "photo" and media.get("url"):
                    photos.append(media["url"])
                elif media.get("preview_image_url"):
                    # video/gif: usamos la miniatura como imagen
                    photos.append(media["preview_image_url"])

            retweets.append(
                {
                    "tweet_id": tweet["id"],
                    "text": text,
                    "photos": photos,
                }
            )

    # Orden cronológico: la API devuelve lo más nuevo primero, lo invertimos
    retweets.reverse()

    newest_id = data.get("meta", {}).get("newest_id")
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


def send_retweet(rt):
    text = rt["text"] if rt["text"] else "📰"
    photos = rt["photos"]

    if not photos:
        send_text(text)
    elif len(photos) == 1:
        send_single_photo(photos[0], text)
    else:
        send_media_group(photos, text)


def main():
    state = load_state()
    data = fetch_new_tweets(state.get("since_id"))
    retweets, newest_id = extract_retweets(data)

    print(f"Retweets nuevos encontrados: {len(retweets)}")

    for rt in retweets:
        send_retweet(rt)
        time.sleep(1)  # pequeño respiro entre mensajes

    if newest_id:
        state["since_id"] = newest_id
        save_state(state)


if __name__ == "__main__":
    main()

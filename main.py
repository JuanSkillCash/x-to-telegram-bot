"""
Vigila los retweets de una cuenta de X (Twitter) y los reenvía a un
tema (topic) específico de un grupo de Telegram.

Variables de entorno necesarias (se configuran como "Secrets" en GitHub Actions):
    X_BEARER_TOKEN        -> Bearer Token de tu app en developer.x.com
    X_USER_ID             -> ID numérico de la cuenta que quieres vigilar
    TELEGRAM_BOT_TOKEN    -> Token que te dio @BotFather
    TELEGRAM_CHAT_ID      -> ID del grupo (negativo, ej: -1001234567890)
    TELEGRAM_THREAD_ID    -> ID del tema "Noticias" dentro del grupo

Guarda el último tweet visto en state.json para no reenviar lo mismo dos veces.
"""

import os
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
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"


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
        "tweet.fields": "created_at,referenced_tweets",
        "expansions": "referenced_tweets.id,referenced_tweets.id.author_id",
        "user.fields": "username",
    }
    if since_id:
        params["since_id"] = since_id

    headers = {"Authorization": f"Bearer {X_BEARER_TOKEN}"}
    resp = requests.get(X_API_URL, headers=headers, params=params, timeout=30)

    if resp.status_code != 200:
        print(f"Error consultando la API de X: {resp.status_code} {resp.text}")
        sys.exit(1)

    return resp.json()


def extract_retweets(data):
    """Devuelve una lista de dicts con la info lista para enviar a Telegram."""
    tweets = data.get("data", [])
    if not tweets:
        return [], data.get("meta", {}).get("newest_id")

    included_tweets = {t["id"]: t for t in data.get("includes", {}).get("tweets", [])}
    included_users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}

    retweets = []
    for tweet in tweets:
        refs = tweet.get("referenced_tweets", [])
        for ref in refs:
            if ref["type"] != "retweeted":
                continue
            original = included_tweets.get(ref["id"])
            if not original:
                continue
            author = included_users.get(original.get("author_id"))
            username = author["username"] if author else None
            link = (
                f"https://x.com/{username}/status/{original['id']}"
                if username
                else f"https://x.com/i/web/status/{original['id']}"
            )
            retweets.append(
                {
                    "tweet_id": tweet["id"],
                    "text": original.get("text", ""),
                    "link": link,
                }
            )

    # Orden cronológico: la API devuelve lo más nuevo primero, lo invertimos
    retweets.reverse()

    newest_id = data.get("meta", {}).get("newest_id")
    return retweets, newest_id


def send_to_telegram(message):
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "disable_web_page_preview": False,
    }
    if TELEGRAM_THREAD_ID:
        payload["message_thread_id"] = TELEGRAM_THREAD_ID

    resp = requests.post(TELEGRAM_API_URL, data=payload, timeout=30)
    if resp.status_code != 200:
        print(f"Error enviando a Telegram: {resp.status_code} {resp.text}")


def main():
    state = load_state()
    data = fetch_new_tweets(state.get("since_id"))
    retweets, newest_id = extract_retweets(data)

    print(f"Retweets nuevos encontrados: {len(retweets)}")

    for rt in retweets:
        message = f"📰 {rt['text']}\n\n{rt['link']}"
        send_to_telegram(message)
        time.sleep(1)  # pequeño respiro entre mensajes

    if newest_id:
        state["since_id"] = newest_id
        save_state(state)


if __name__ == "__main__":
    main()

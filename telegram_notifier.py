import os
import logging
import requests

logger = logging.getLogger(__name__)

class Notifier:
    def __init__(self):
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    def send_message(self, text: str):
        if not self.token or not self.chat_id:
            logger.error("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing from environment variables.")
            raise ValueError("Missing Telegram credentials in environment variables.")

        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False
        }

        response = requests.post(url, json=payload, timeout=10)
        
        if not response.ok:
            logger.error(f"Failed to send message: {response.content}")
            response.raise_for_status()

        return response.json()

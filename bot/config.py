import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
STORE_NAME = os.getenv("STORE_NAME", "VEXA SHOPPING")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "vexashopping_suporte")
CHANNEL_ID = os.getenv("CHANNEL_ID", "")  # ex: @vexashopping (avisos de venda/estoque)
DB_PATH = os.getenv("DB_PATH", "data/store.db")

# Binance Pay (Merchant) - https://merchant.binance.com
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_SECRET = os.getenv("BINANCE_SECRET", "")
BINANCE_CURRENCY = os.getenv("BINANCE_CURRENCY", "USDT")
ORDER_TIMEOUT_MIN = int(os.getenv("ORDER_TIMEOUT_MIN", "30"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "15"))
SANDBOX = os.getenv("SANDBOX", "0") == "1"  # 1 = simula pagamentos (testes)

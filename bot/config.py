import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
STORE_NAME = os.getenv("STORE_NAME", "VEXA SHOPPING")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "")
CHANNEL_ID = os.getenv("CHANNEL_ID", "")
DB_PATH = os.getenv("DB_PATH", "data/store.db")
SANDBOX = os.getenv("SANDBOX", "0") == "1"  # simula pagamentos aprovados
ORDER_TIMEOUT_MIN = int(os.getenv("ORDER_TIMEOUT_MIN", "30"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "10"))

# Pix - Asaas (https://www.asaas.com > Integrações > Chave de API)
ASAAS_API_KEY = os.getenv("ASAAS_API_KEY", "")
ASAAS_ENV = os.getenv("ASAAS_ENV", "production")  # production | sandbox

# USDT - Binance Pay Merchant (https://merchant.binance.com)
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_SECRET = os.getenv("BINANCE_SECRET", "")
USDT_BRL_FALLBACK = float(os.getenv("USDT_BRL_FALLBACK", "5.80"))
USDT_SPREAD = float(os.getenv("USDT_SPREAD", "0.02"))  # 2% sobre a cotação

# Fidelidade
REF_BONUS_PCT = float(os.getenv("REF_BONUS_PCT", "5"))  # % da compra do indicado vira saldo de quem indicou
LEVELS = [(0, "Bronze", "●"), (200, "Prata", "●●"), (1000, "Ouro", "●●●"), (5000, "Black", "◆")]

"""Настройки программы.

Все настройки лежат в файле settings.env рядом с программой (или с папкой проекта).
Формат простой: КЛЮЧ=значение. Если файла нет — он создаётся с настройками по умолчанию.
Переменные окружения имеют приоритет над файлом.
"""
import os
import sys
from pathlib import Path

# Папка с данными:
#   Windows (MiniPOS.exe)  — рядом с программой
#   Mac (MiniPOS.app)      — Документы/MiniPOS (внутри .app хранить данные нельзя)
#   запуск из кода         — папка проекта
if getattr(sys, "frozen", False):
    if sys.platform == "darwin":
        DATA_DIR = Path.home() / "Documents" / "MiniPOS"
    else:
        DATA_DIR = Path(sys.executable).resolve().parent
else:
    DATA_DIR = Path(__file__).resolve().parent.parent
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Папка с интерфейсом (внутри собранного .exe она распаковывается во временную папку)
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
STATIC_DIR = BUNDLE_DIR / "app" / "static"

SETTINGS_FILE = DATA_DIR / "settings.env"

DEFAULT_SETTINGS = """\
# ===== Настройки MiniPOS =====
# После изменения перезапустите программу.

# Тестовые данные (товары из каталога mizon yalyan, тестовые оптовики) при первом запуске
# с пустой базой: 1 — создать, 0 — начать с пустой базы
DEMO_DATA=__DEMO__

# --- Цены и оптовые клиенты ---
# Розничная цена = оптовая × коэффициент, округлённая до RETAIL_ROUND тенге
RETAIL_MARKUP=1.5
RETAIL_ROUND=100
# Условия для оптовиков: первый закуп и ежемесячный минимум, в тенге (0 = без условия)
WHOLESALE_FIRST_MIN=100000
WHOLESALE_MONTHLY_MIN=30000
# Максимальная скидка на чек, % (защита от опечатки)
MAX_DISCOUNT_PERCENT=30

# --- ИИ ---
# Бесплатно: Ollama на этом же компьютере (https://ollama.com)
#   после установки выполните в терминале:  ollama pull qwen2.5:7b
# Если компьютер слабый (меньше 16 ГБ памяти) — попробуйте qwen2.5:3b
AI_BASE_URL=http://localhost:11434/v1
AI_MODEL=qwen2.5:7b
AI_API_KEY=ollama
# Чтобы вместо Ollama использовать OpenAI:
#   AI_BASE_URL=https://api.openai.com/v1
#   AI_MODEL=gpt-4o-mini
#   AI_API_KEY=sk-...

# --- Telegram-бот (необязательно) ---
# Токен от @BotFather. Пусто = бот выключен.
TELEGRAM_BOT_TOKEN=
# Telegram ID тех, кому можно пользоваться ботом, через запятую.
# Свой ID бот покажет, если написать ему /start.
TELEGRAM_ALLOWED_IDS=

# --- Резервные копии ---
# Папка для ежедневной копии базы. Можно указать папку iCloud / Google Диска.
# Пусто = папка backups рядом с программой.
BACKUP_DIR=
BACKUP_KEEP_DAYS=30
"""


# в собранной программе (для магазинов) — пустая база, при запуске из кода — тестовые данные
DEFAULT_SETTINGS = DEFAULT_SETTINGS.replace("__DEMO__", "0" if getattr(sys, "frozen", False) else "1")

PRICING_BLOCK = DEFAULT_SETTINGS[DEFAULT_SETTINGS.index("# --- Цены"):DEFAULT_SETTINGS.index("# --- ИИ")]


def _load() -> dict:
    try:
        if not SETTINGS_FILE.exists():
            SETTINGS_FILE.write_text(DEFAULT_SETTINGS, encoding="utf-8")
        elif "RETAIL_MARKUP" not in SETTINGS_FILE.read_text(encoding="utf-8"):
            # файл от старой версии — дописываем новые настройки, ничего не трогая
            with SETTINGS_FILE.open("a", encoding="utf-8") as f:
                f.write("\n" + PRICING_BLOCK)
    except OSError:
        pass
    values = {}
    text = SETTINGS_FILE.read_text(encoding="utf-8") if SETTINGS_FILE.exists() else DEFAULT_SETTINGS
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip()
    return values


_file = _load()


def get(key: str, default: str = "") -> str:
    return os.getenv(key) or _file.get(key) or default


DEMO_DATA = get("DEMO_DATA", "0" if getattr(sys, "frozen", False) else "1") == "1"

DATABASE_URL = get("DATABASE_URL", f"sqlite:///{(DATA_DIR / 'minipos.db').as_posix()}")

RETAIL_MARKUP = float(get("RETAIL_MARKUP", "1.5").replace(",", ".") or 1.5)
RETAIL_ROUND = int(get("RETAIL_ROUND", "100") or 100)
WHOLESALE_FIRST_MIN = int(get("WHOLESALE_FIRST_MIN", "100000") or 0)
WHOLESALE_MONTHLY_MIN = int(get("WHOLESALE_MONTHLY_MIN", "30000") or 0)
MAX_DISCOUNT_PERCENT = int(get("MAX_DISCOUNT_PERCENT", "30") or 30)

AI_BASE_URL = get("AI_BASE_URL", "http://localhost:11434/v1").rstrip("/")
AI_MODEL = get("AI_MODEL", "qwen2.5:7b")
AI_API_KEY = get("AI_API_KEY", "ollama")

TELEGRAM_BOT_TOKEN = get("TELEGRAM_BOT_TOKEN")
TELEGRAM_ALLOWED_IDS = {int(x) for x in get("TELEGRAM_ALLOWED_IDS").replace(" ", "").split(",") if x.isdigit()}

BACKUP_DIR = Path(get("BACKUP_DIR") or DATA_DIR / "backups")
BACKUP_KEEP_DAYS = int(get("BACKUP_KEEP_DAYS", "30") or 30)

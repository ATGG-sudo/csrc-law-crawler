"""爬虫配置。"""

from settings import SETTINGS

BASE_URL = "https://neris.csrc.gov.cn/falvfagui/"
AMAC_BASE_URL = "https://www.amac.org.cn/"
AMAC_RULES_BASE_URL = "https://fg.amac.org.cn/"
OUTPUT_DIR = SETTINGS.output_root
RAW_SUBDIR = "raw"
WORK_SUBDIR = "work"
CANONICAL_SUBDIR = "canonical"
REPORTS_SUBDIR = "reports"

# 接近真人：每次请求间隔（秒）
DELAY_MIN = SETTINGS.delay_min
DELAY_MAX = SETTINGS.delay_max

# 每抓取 N 条法规后额外休息（秒）
BATCH_SIZE = SETTINGS.batch_size
BATCH_PAUSE_MIN = SETTINGS.batch_pause_min
BATCH_PAUSE_MAX = SETTINGS.batch_pause_max

# 失败重试
MAX_RETRIES = SETTINGS.max_retries
RETRY_BACKOFF_BASE = SETTINGS.retry_backoff_base

PAGE_SIZE = 20
LAW_TYPE_REGULATION = 1
LAW_TYPE_WRIT = 2

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

MAX_DOWNLOAD_BYTES = SETTINGS.max_download_bytes
AMAC_VERIFY_TLS = SETTINGS.amac_verify_tls
WORKERS = SETTINGS.workers

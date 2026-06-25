"""爬虫配置。"""

from pathlib import Path

BASE_URL = "https://neris.csrc.gov.cn/falvfagui/"
AMAC_BASE_URL = "https://www.amac.org.cn/"
AMAC_RULES_BASE_URL = "https://fg.amac.org.cn/"
OUTPUT_DIR = Path("/mnt/d/FUND_COMPLIANCE/CSRC")
RAW_SUBDIR = "raw"
WORK_SUBDIR = "work"
CANONICAL_SUBDIR = "canonical"
REPORTS_SUBDIR = "reports"

# 接近真人：每次请求间隔（秒）
DELAY_MIN = 1.8
DELAY_MAX = 3.6

# 每抓取 N 条法规后额外休息（秒）
BATCH_SIZE = 40
BATCH_PAUSE_MIN = 8.0
BATCH_PAUSE_MAX = 15.0

# 失败重试
MAX_RETRIES = 5
RETRY_BACKOFF_BASE = 5.0

PAGE_SIZE = 20
LAW_TYPE_REGULATION = 1
LAW_TYPE_WRIT = 2

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

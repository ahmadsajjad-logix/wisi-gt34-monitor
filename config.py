from pathlib import Path


# ============================================================
# PROJECT PATHS
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

DATABASE_DIR = BASE_DIR / "database"
LOG_DIR = BASE_DIR / "logs"

DATABASE_PATH = DATABASE_DIR / "wisi_monitor.db"
LOG_PATH = LOG_DIR / "wisi_monitor.log"


# ============================================================
# WISI CHASSIS
# ============================================================

WISI_HOST = "192.168.3.27"

WISI_MODULES = {
    1: {
        "name": "GT34 Module 1",
        "remote": "169_254_40_216",
        "remote_ip": "169.254.40.216",
    },
    5: {
        "name": "GT34 Module 5",
        "remote": "169_254_27_216",
        "remote_ip": "169.254.27.216",
    },
}


# ============================================================
# POLLING
# ============================================================

POLL_INTERVAL_SECONDS = 5
HTTP_TIMEOUT_SECONDS = 10


# ============================================================
# WISI RESOURCES
# ============================================================

RESOURCE_TUNER_FLUX = "tuner_flux.xmlc"

RESOURCE_TS_FLUX = (
    "tsio/inputs_conf_flux.xmlc"
)

RESOURCE_PIDMAPPER = (
    "tsio/pidmapper.xmlc"
)

RESOURCE_PCR = (
    "tsio/input_regulator/inputs.xmlc"
)

RESOURCE_TSDB_INPUT = (
    "tsdb/input.xmlc"
)
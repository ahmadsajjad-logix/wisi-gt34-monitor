from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATABASE_DIR = BASE_DIR / "database"
LOG_DIR = BASE_DIR / "logs"
DATABASE_PATH = DATABASE_DIR / "wisi_monitor.db"
LOG_PATH = LOG_DIR / "wisi_monitor.log"

WISI_CHASSIS = {
    "192.168.3.27": {"name": "WISI-IRD-01", "modules": {
        1: {"name": "GT34 Module 1", "remote": "169_254_40_216", "remote_ip": "169.254.40.216"},
        5: {"name": "GT34 Module 5", "remote": "169_254_27_216", "remote_ip": "169.254.27.216"},
    }},
    "192.168.3.45": {"name": "WISI-IRD-02", "modules": {
        1: {"name": "GT34 Module 1", "remote": "169_254_64_216", "remote_ip": "169.254.64.216"},
        5: {"name": "GT34 Module 5", "remote": "169_254_90_216", "remote_ip": "169.254.90.216"},
    }},
    "192.168.3.8": {"name": "WISI-IRD-03", "modules": {
        1: {"name": "GT34 Module 1", "remote": "169_254_93_216", "remote_ip": "169.254.93.216"},
        5: {"name": "GT34 Module 5", "remote": "169_254_131_216", "remote_ip": "169.254.131.216"},
    }},
    "192.168.3.9": {"name": "WISI-IRD-04", "modules": {
        1: {"name": "GT34 Module 1", "remote": "169_254_60_216", "remote_ip": "169.254.60.216"},
        5: {"name": "GT34 Module 5", "remote": "169_254_138_215", "remote_ip": "169.254.138.215"},
    }},
}

# Backward compatibility for older helper scripts.
WISI_HOST = "192.168.3.27"
WISI_MODULES = WISI_CHASSIS[WISI_HOST]["modules"]

POLL_INTERVAL_SECONDS = 5
HTTP_TIMEOUT_SECONDS = 10
RESOURCE_TUNER_FLUX = "tuner_flux.xmlc"
RESOURCE_TS_FLUX = "tsio/inputs_conf_flux.xmlc"
RESOURCE_PIDMAPPER = "tsio/pidmapper.xmlc"
RESOURCE_PCR = "tsio/input_regulator/inputs.xmlc"
RESOURCE_TSDB_INPUT = "tsdb/input.xmlc"

# ---------------------------------------------------------------------------
# Wellav CMP201
# ---------------------------------------------------------------------------
# Baseboard / management chassis: 192.168.3.177
#
# Each CR2-DVBS2CI-00 module has its own directly reachable IP address.
# These addresses are acquisition topology only; administrative TV43 carrier
# identities remain defined independently in the existing TV43 configuration.
#
# No username/password is stored here.
WELLAV_CMP201 = {
    "name": "Wellav CMP201",
    "baseboard_ip": "192.168.3.177",
    "chassis_type": 1,
    "modules": {
        1: {
            "name": "CR2-DVBS2CI-00 Module 1",
            "ip": "192.168.3.176",
        },
        2: {
            "name": "CR2-DVBS2CI-00 Module 2",
            "ip": "192.168.3.175",
        },
        3: {
            "name": "CR2-DVBS2CI-00 Module 3",
            "ip": "192.168.3.174",
        },
        4: {
            "name": "CR2-DVBS2CI-00 Module 4",
            "ip": "192.168.3.173",
        },
        5: {
            "name": "CR2-DVBS2CI-00 Module 5",
            "ip": "192.168.3.172",
        },
        6: {
            "name": "CR2-DVBS2CI-00 Module 6",
            "ip": "192.168.3.171",
        },
    },
}

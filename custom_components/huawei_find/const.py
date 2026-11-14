"""Constants for the Huawei Cloud Find Device integration."""

DOMAIN = "huawei_find"

BASE_URL = "https://cloud.huawei.com"
EP_DEVICE_LIST = "/findDevice/getMobileDeviceList"
EP_SHARE_GRANTS = "/findDevice/getShareGrantInfo"
EP_LOCATE = "/findDevice/locate"
EP_QUERY = "/findDevice/queryLocateResult"
EP_HEARTBEAT = "/heartbeatCheck"
EP_BELL = "/findDevice/portalBellReq"

CONF_COOKIE = "cookie"
CONF_DEVICES = "devices"
CONF_AUTH_TIME = "auth_time"
CONF_GEOCODE_PROVIDER = "geocode_provider"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_AMAP_KEY = "amap_key"

GEOCODE_NONE = "none"
GEOCODE_AMAP = "amap"
GEOCODE_OSM = "osm"
GEOCODE_HUAWEI = "huawei"
GEOCODE_AUTO = "auto"

# Huawei\u0027s own map service (embedded key from the web portal, no login needed)
HUAWEI_GEOCODE_URL = (
    "https://siteapi.cloud.huawei.com.cn/mapApi/v3/siteService/reverseGeocode"
)
HUAWEI_GEOCODE_KEY = (
    "CV8QPlgNpxJ1tuEj/bQ3GC+pfiOJF2tqOznakta22VByGNFk"
    "IGrLRWOkAMgf3WlKuEVEGm9FPD1LjFCMB5ce6gz4/aIm"
)

SERVICE_REFRESH = "refresh"
SERVICE_SET_COOKIE = "set_cookie"

# Ring (find my phone) parameters
RING_COOLDOWN = 30  # seconds between rings for the same device
RING_RETRIES = 1  # Huawei push delivery can fail once while the phone wakes up
RING_RETRY_DELAY = 3  # seconds before retrying a failed ring push
HW_MODEL = "noh-an00"  # x-hw-model header the portal sends

# Warn the user before the observed ~7 day session lifetime runs out
EXPIRY_WARNING_DAYS = 6

ATTR_DEVICE_ID = "device_id"
ATTR_LATITUDE = "latitude"
ATTR_LONGITUDE = "longitude"
ATTR_ACCURACY = "accuracy"
ATTR_BATTERY = "battery"
ATTR_STATUS = "status"
ATTR_ERROR = "last_error"
ATTR_COORD_SYS = "coordinate_system"
ATTR_DEVICE_TIME = "device_time"
ATTR_ADDRESS = "address"
ATTR_CHARGING = "battery_charging"
ATTR_SCREEN_LOCKED = "screen_locked"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Concurrency / retry for locate requests (Huawei throttles bursts)
MAX_CONCURRENT_LOCATES = 3
LOCATE_RETRIES = 1
LOCATE_RETRY_DELAY = 2.0

# Reverse geocoding: OSM Nominatim asks for max 1 request/second
OSM_MIN_INTERVAL = 1.1
GEOCODE_CACHE_TTL = 600
# Keep the previous address when the device has not really moved
ADDRESS_KEEP_RADIUS = 300  # meters

# Maximum time to wait for a locate result (seconds) and polling interval.
LOCATE_TIMEOUT = 30
POLL_INTERVAL = 2.0

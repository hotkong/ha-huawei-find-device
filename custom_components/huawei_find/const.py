"""Constants for the Huawei Cloud Find Device integration."""

DOMAIN = "huawei_find"

BASE_URL = "https://cloud.huawei.com"
EP_DEVICE_LIST = "/findDevice/getMobileDeviceList"
EP_SHARE_GRANTS = "/findDevice/getShareGrantInfo"
EP_LOCATE = "/findDevice/locate"
EP_QUERY = "/findDevice/queryLocateResult"

CONF_COOKIE = "cookie"
CONF_DEVICES = "devices"
CONF_AUTH_TIME = "auth_time"
CONF_GEOCODE_PROVIDER = "geocode_provider"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_AMAP_KEY = "amap_key"

GEOCODE_NONE = "none"
GEOCODE_AMAP = "amap"
GEOCODE_OSM = "osm"

SERVICE_REFRESH = "refresh"

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

# Maximum time to wait for a locate result (seconds) and polling interval.
LOCATE_TIMEOUT = 30
POLL_INTERVAL = 2.0

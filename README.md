# Huawei Cloud Find Device

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![CI](https://github.com/hotkong/ha-huawei-find-device/actions/workflows/ci.yml/badge.svg)](https://github.com/hotkong/ha-huawei-find-device/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

把华为云空间「查找设备」接入 Home Assistant：**点一个按钮，让手机立刻上报一次位置**。

Trigger a Huawei Cloud *Find Device* query from Home Assistant — press a button and
the phone is asked to report its location right away.

> 非官方集成，使用华为云网页版接口，接口变更可能导致失效。请仅用于自己或已授权的家人设备。  
> Unofficial integration built on the Huawei Cloud web API. It may break if Huawei
> changes the endpoints. Use it only for devices you own or are authorized to track.

---

## 中文说明

### 为什么需要它

华为手机装 Home Assistant Companion App 后，定位经常因为省电策略而延迟。
在华为云网页上点一次「查找设备 → 定位」，会唤醒手机侧定位服务，随后 Companion App
也会跟着更新。本集成把这一步自动化成了 HA 里的按钮。

### 功能

- 一个「全部刷新」按钮 + 每台设备一个「刷新定位」按钮（自有 + 家人共享）；
- 每台设备一个定位传感器：状态显示地址（或 `纬度, 经度`），属性含坐标、精度、电量、是否充电、是否锁屏；
- 每台设备一个 `device_tracker`（GPS 源），可直接绑定到 `person`，在原生地图上显示；
- 结果持久化，HA 重启后保留上次位置；单次查询失败时**保留上次坐标**并标记 `stale: true`，不会把实体清空；
- Cookie 自动回存（华为云会轮换会话 Cookie）+ 每 6 小时保活 + 失效时弹通知并引导重新授权；
- 可选地址反查：高德 Web 服务 或 OpenStreetMap Nominatim；
- 可选自动刷新间隔（0 = 关闭），不想写自动化也能定时查询。

### 安装

**HACS（自定义仓库）**

1. HACS → 集成 → 右上角 ⋮ → 自定义存储库；
2. 地址填本仓库 URL，类别选「Integration」；
3. 安装后重启 Home Assistant。

**手动**

把 `custom_components/huawei_find` 复制到 HA 配置目录的 `custom_components/` 下，重启。

### 配置

1. 电脑浏览器登录 <https://cloud.huawei.com/>，打开「查找设备」并点一次定位；
2. 按 `F12` → **Network**，过滤 `findDevice`；
3. 右键任意请求 → **Copy → Copy request headers**，找到 `Cookie:` 那一行；
4. 回到 HA：设置 → 设备与服务 → 添加集成 → **Huawei Cloud Find Device**，粘贴 Cookie；
5. 选择要跟踪的设备，提交。

> 必须从请求头复制，`document.cookie` 拿不到 `HttpOnly` 的 `JSESSIONID`。

### 实体与服务

| 类型 | 说明 |
|---|---|
| `button.<...>_refresh_all` | 刷新全部定位（一次查询所有设备） |
| `button.<device>_refresh` | 单台设备刷新定位 |
| `sensor.<device>_last_location` | 状态 = 地址（无地址时 `纬度, 经度`） |
| `device_tracker.<device>` | GPS 追踪器，可绑定 `person` |
| 服务 `huawei_find.refresh` | 同按钮，但等全部设备查完才返回 |

### 地址反查

设置 → 设备与服务 → Huawei Cloud Find Device → **配置**：

- `AMap / Gaode`：需要[高德开放平台](https://lbs.amap.com/) Web 服务 Key，国内地址最准；
- `OpenStreetMap`：免 Key，国内地址较粗；
- 不选则只保留坐标。
### 自动刷新

同一个配置页可设置**自动刷新间隔**（分钟）：

- `0` = 关闭（默认），只在点按钮或调服务时查询；
- `N > 0` = 每隔 N 分钟自动刷新全部设备。

> 不要同时启用本集成间隔和外部自动化，否则会重复唤醒手机。

### 自动化示例

```yaml
automation:
  - alias: Refresh Huawei locations
    triggers:
      - trigger: time_pattern
        minutes: /30
    conditions:
      - condition: time
        after: "06:00:00"
        before: "22:30:00"
    actions:
      - action: button.press
        target:
          entity_id: button.huawei_cloud_find_device_refresh_all
```

---

## English

### Why

The HA Companion app frequently reports stale locations on Huawei phones because of
aggressive battery management. Querying *Find Device* on the Huawei Cloud portal
wakes the phone's location service. This integration turns that into a HA button.

### Features

- One "refresh all" button plus a per-device "refresh location" button (own + family-shared);
- Per-device location sensor: state = address (or `lat, lon`), attributes include
  coordinates, accuracy, battery, charging and lock-screen state;
- Per-device GPS `device_tracker`, ready to be attached to a `person`;
- Last results survive a HA restart; a failed refresh keeps the previous
  coordinates and flags them as `stale`;
- Cookie rotation is persisted automatically, a 6-hour keepalive runs in the
  background, and a notification prompts re-authentication when the session expires;
- Optional reverse geocoding via AMap or OpenStreetMap;
- Optional automatic refresh interval (0 = disabled).

### Installation

HACS → Integrations → ⋮ → Custom repositories → add this repo URL, category
*Integration*. Or copy `custom_components/huawei_find` into your HA config and restart.

### Configuration

Copy the `Cookie` request header (DevTools → Network → any `findDevice` request)
and paste it into the integration setup, then pick the devices to track.

### Options

Open the integration options to choose a reverse geocoding provider and an
automatic refresh interval in minutes (0 disables it).

### Services

`huawei_find.refresh` — same as pressing the button, but waits for completion.

---

## 工作原理 / How it works

```
POST https://cloud.huawei.com/findDevice/getMobileDeviceList   {"deviceType": 0}
POST https://cloud.huawei.com/findDevice/getShareGrantInfo     {"traceId": "..."}
POST https://cloud.huawei.com/findDevice/locate                {"deviceId", "traceId", ...}
POST https://cloud.huawei.com/findDevice/queryLocateResult     {"deviceId", "sequence", ...}
```

认证使用网页登录态 Cookie（`JSESSIONID` + `CSRFToken` + `userId` 等），不保存华为账号密码。
坐标会按 `sysType` 从 GCJ-02/BD-09 转成 WGS-84。

## 已知限制 / Limitations

- Cookie 会过期，需要重新粘贴；没有 OAuth refresh token；
- 手表/手环常返回 `990 / device inactive`，属正常现象；
- 接口返回的坐标可能是云端缓存，无法区分新鲜度；
- 不提供定时定位（由用户用自动化控制）。

## 免责声明 / Disclaimer

本项目与华为无任何关联。请遵守华为服务条款，仅追踪自己或已获授权的设备。

## Roadmap / 路线图

已完成：按钮触发、定位传感器、GPS 追踪器、地址反查、自动刷新间隔、失败保留最后坐标、并发限流与逆地理缓存。

- [ ] 自动刷新时间窗（`start_hour` / `end_hour`）
- [ ] 设备列表热更新（不重载集成即可识别新增/移除设备）
- [ ] `diagnostics.py`（脱敏导出，便于排查）
- [ ] 迁移到 `entry.runtime_data`
- [ ] 单元测试 + GitHub Actions CI
- [ ] `icons.json` / `pyproject.toml` / `info.md`

## License

[MIT](LICENSE)

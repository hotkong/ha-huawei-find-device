# Huawei Cloud Find Device

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
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
也会跟着更新。本集成把这一步自动化成了 HA 里的按钮，并且直接读取华为云返回的坐标。

### 功能

- 一个「全部刷新」按钮 + 每台设备一个「刷新定位」按钮（自有设备 + 家人共享设备）；
- 定位传感器：状态显示地址（未配置地址反查时显示 `纬度, 经度`）；
- 电量、充电状态、上次定位状态各自独立成实体，便于做自动化；
- 每台设备一个 `device_tracker`（GPS 源），可绑定到 `person`，在原生地图显示；
- `huawei_find.refresh` 服务，方便脚本/自动化调用；
- 结果落盘：HA 重启后保留上次位置；**单次定位失败不会清空坐标**，而是沿用上次结果并标记 `stale`；
- Cookie 轮换自动回存 + 每 5 分钟 `heartbeatCheck` 保活（与网页版同机制）+ 失效时弹通知引导重新授权；
- 可选自动刷新间隔（分钟，0 = 关闭）；
- 可选地址反查：高德 Web 服务 或 OpenStreetMap；
- 自动跳过在界面上**已禁用**的设备，不再无谓唤醒它们。

### 安装

**HACS（自定义仓库）**

1. HACS → 集成 → 右上角 ⋮ → 自定义存储库；
2. 地址填 `https://github.com/hotkong/ha-huawei-find-device`，类别选「Integration」；
3. 安装后重启 Home Assistant。

**手动**

把 `custom_components/huawei_find` 复制到 HA 配置目录的 `custom_components/` 下，重启。

要求：Home Assistant 2024.6+，纯 Python，无额外依赖（aarch64 / amd64 均可）。

### 配置

1. 电脑浏览器登录 <https://cloud.huawei.com/>，打开「查找设备」并点一次定位；
2. 按 `F12` → **Network**，过滤 `findDevice`；
3. 右键任意请求 → **Copy → Copy request headers**，找到 `Cookie:` 那一行；
4. 回到 HA：设置 → 设备与服务 → 添加集成 → **Huawei Cloud Find Device**，粘贴 Cookie；
5. 选择要跟踪的设备，提交。

> 必须从请求头复制，浏览器控制台里的 `document.cookie` 拿不到 `HttpOnly` 的 `JSESSIONID`。

### 实体

实体 ID 会随界面语言、设备名不同而不同，这里只列类型：

| 实体 | 说明 |
|---|---|
| `button.<entry>_refresh_all` | 刷新全部定位（跳过已禁用设备） |
| `button.<device>_refresh` | 单台设备刷新定位 |
| `sensor.<device>_last_location` | 状态 = 地址；无地址时 `纬度, 经度` |
| `sensor.<device>_battery` | 电量（`device_class: battery`，诊断类） |
| `binary_sensor.<device>_charging` | 充电状态（诊断类） |
| `sensor.<device>_last_locate_status` | 上次定位状态（枚举：成功 / 失败 / 未知，诊断类） |
| `device_tracker.<device>` | GPS 追踪器，可绑定 `person` |
| 服务 `huawei_find.refresh` | 同「全部刷新」，但等全部设备查完才返回 |

定位传感器的属性里还有：`latitude`、`longitude`、`accuracy`、`coordinate_system`、`address`、`address_stale`、`device_time`、`located_at`、`last_attempt`、`status`、`last_error`。

### 配置项

设置 → 设备与服务 → Huawei Cloud Find Device → **配置**：

| 选项 | 说明 |
|---|---|
| 地址来源 | `AMap / Gaode`（需 [高德开放平台](https://lbs.amap.com/) Web 服务 Key，国内最准）/ `OpenStreetMap`（免 Key）/ 不查地址 |
| 高德 Key | 仅选择高德时需要 |
| 自动刷新间隔 | 分钟，`0` = 关闭（默认）；最大 720 |

> 自动刷新是**全天候**的。如果需要「只在 6:00–22:00 每 30 分钟」这类时间窗，把间隔设为 0，用下面的自动化。

### 自动化示例

```yaml
alias: 华为云定位定时刷新
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
mode: single
```

实体 ID 请以你系统里的实际值为准。**不要**同时启用集成里的自动间隔和这条自动化，否则会重复唤醒手机。

### 禁用 vs 隐藏

- **禁用**设备/实体：不再加载，且「全部刷新」会跳过它，不产生请求；
- **隐藏**：只是不在仪表盘显示，仍然正常刷新。

### 工作原理

网页版「查找设备」就是几个 JSON 接口，鉴权靠网页 Cookie，因此不需要浏览器：

```text
POST /findDevice/getMobileDeviceList   {"deviceType": 0}
POST /findDevice/getShareGrantInfo     {"traceId": ...}
POST /findDevice/locate                {device, traceId, ...}
POST /findDevice/queryLocateResult     {device, sequence, ...}
GET  /heartbeatCheck?checkType=1       # 每 5 分钟，保持会话并轮换 CSRF Token
```

几个实现细节：

- 家人共享设备不在设备列表接口里，要单独取授权列表，并在定位时带 `senderUserId` + `relationType: 2`；
- 返回码 `990` 是双重语义（登录失效 **或** 设备级失败），必须再用设备列表接口探活区分；
- 顶层 `latitude/longitude` 实测为 **BD-09**，`latitude_WGS/longitude_WGS` 是 WGS-84，均已转换；
- 华为会轮换 `JSESSIONID` / `token` / `CSRFToken`，收到 `Set-Cookie` 必须回存，否则下一次请求就掉线。

### 已知限制

- Cookie 有有效期，没有 OAuth 续期；心跳保活能显著延长，但改密码、异地登录、风控仍会使其失效，届时需重新粘贴；
- 手表/手环常年离线，返回 `device inactive` 属正常；
- 接口返回的坐标可能是云端缓存，无法区分「刚上报」与「上次缓存」；
- 地址反查依赖第三方服务，偶发超时会沿用上次地址（`address_stale: true`）。

### Roadmap

- [x] 按钮触发 / 定位传感器 / GPS 追踪器
- [x] 地址反查（高德、OSM）
- [x] 自动刷新间隔
- [x] 失败保留最后坐标、跳过禁用设备
- [x] 心跳保活、Cookie 轮换回存
- [x] 单元测试（`python -m unittest discover -s tests`）
- [ ] 自动刷新的时间窗（`start_hour` / `end_hour`）
- [ ] 设备列表热更新（不重载集成）
- [ ] diagnostics 脱敏导出
- [ ] 迁移到 `entry.runtime_data`

---

## English

### Why

The HA Companion app frequently reports stale locations on Huawei phones because of
aggressive battery management. Querying *Find Device* on the Huawei Cloud portal wakes
the phone's location service. This integration turns that into a HA button and also
reads the coordinates Huawei returns.

### Features

- One "refresh all" button plus a per-device "refresh location" button;
- Location sensor (address, or `lat, lon` without reverse geocoding);
- Separate battery, charging and last-locate-status entities;
- GPS `device_tracker` per device, ready to attach to a `person`;
- `huawei_find.refresh` service for scripts;
- Results survive restarts; a failed locate keeps the last known position and marks it
  `stale` instead of clearing the entity;
- Cookie rotation is persisted, a 5-minute `heartbeatCheck` keeps the session alive, and
  an expired session raises a re-auth notification;
- Optional auto refresh interval (0 disables it);
- Optional reverse geocoding via AMap or OpenStreetMap;
- Devices disabled in the UI are skipped automatically.

### Installation

HACS → Integrations → ⋮ → Custom repositories → add this repo URL, category
*Integration*. Or copy `custom_components/huawei_find` into your HA config and restart.

Requirements: Home Assistant 2024.6+, no extra dependencies.

### Configuration

Copy the `Cookie` request header (DevTools → Network → any `findDevice` request) and
paste it into the integration setup, then pick the devices to track.

### Entities

`button.*_refresh_all`, `button.*_refresh`, `sensor.*_last_location`,
`sensor.*_battery`, `binary_sensor.*_charging`, `sensor.*_last_locate_status`,
`device_tracker.*`, plus the `huawei_find.refresh` service.

### Known limitations

- The cookie expires eventually; the heartbeat keeps it alive much longer but can still
  be invalidated by a password change, a new login or risk control;
- Watches/bands are usually offline and return `device inactive`;
- Returned coordinates may come from Huawei's cache;
- Reverse geocoding is best effort and falls back to the previous address.

## License

[MIT](LICENSE)

# Huawei Cloud Find Device

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

把华为云空间「查找设备」接入 Home Assistant：**点一个按钮，让手机立刻上报一次位置**。

Trigger a Huawei Cloud *Find Device* query from Home Assistant — press a button and the
phone is asked to report its location right away.

> 非官方集成，依赖华为云网页版，接口调整可能导致失效。请仅用于自己或已授权的家人设备。  
> Unofficial integration built on the Huawei Cloud web portal. It may break if Huawei
> changes it. Use it only for devices you own or are authorized to track.

---

## 中文说明

### 为什么需要它

华为手机装 Home Assistant Companion App 后，定位经常因为省电策略而延迟。
在华为云网页上点一次「查找设备 → 定位」，会唤醒手机侧定位服务，随后 Companion App
也会跟着更新。本集成把这一步自动化成了 HA 里的按钮，并直接读取华为云返回的位置。

### 功能

- 一个「全部刷新」按钮 + 每台设备一个「刷新定位」按钮（自有设备与家人共享设备都支持）；
- 每台自有手机一个「响铃」按钮：找回手机时触发最大音量警报，30 秒冷却；
- 定位传感器：状态显示地址；配置了地址源后不会再显示经纬度，反查失败时显示「地址解析失败」（坐标仍保留在属性里）；
- 电量、充电状态、上次定位状态各自独立成实体，便于做自动化；
- 每台设备一个 GPS 追踪器，可绑定到 `person`，在原生地图显示；
- 可选自动刷新间隔，到点自动查询全部设备；
- 地址反查支持多种来源，默认自动降级；切换来源后已有坐标会立即重新解析（不会唤醒手机）；
- 选项修改后立即生效，不需要重启或重载集成；
- 单次定位失败不会清空位置：沿用上次结果并标记为「过期」，HA 重启后也保留；
- 会话状态可查看（「全部刷新」按钮属性），临近过期会提醒，失效会弹通知引导重新授权；
- 在界面上禁用的设备会被自动跳过，不再无谓唤醒；
- 支持配置多个华为账号，各自独立、互不影响。

### 安装

**HACS（自定义仓库）**

1. HACS → 集成 → 右上角 ⋮ → 自定义存储库；
2. 地址填 `https://github.com/hotkong/ha-huawei-find-device`，类别选「Integration」；
3. 安装后重启 Home Assistant。

**手动**

把 `custom_components/huawei_find` 复制到 HA 配置目录的 `custom_components/` 下，重启。

要求：Home Assistant 2024.6+，纯 Python，无额外依赖。

### 配置

1. 电脑浏览器登录 <https://cloud.huawei.com/>，打开「查找设备」并点一次定位；
2. 按 `F12` → **Network**，过滤 `locate`；
3. 右键任意请求 → **Copy → Copy request headers**，找到 `Cookie:` 那一行；
4. 回到 HA：设置 → 设备与服务 → 添加集成 → **Huawei Cloud Find Device**，粘贴 Cookie；
5. 勾选要跟踪的设备，提交。

> 必须从请求头复制：浏览器控制台里的 `document.cookie` 拿不到登录态的完整信息。

### 实体

实体 ID 会随界面语言、设备名不同而不同，这里只列类型：

| 实体 | 说明 |
|---|---|
| `button.<account>_refresh_all` | 刷新全部定位（跳过已禁用设备），属性里可看会话状态 |
| `button.<device>_refresh` | 单台设备刷新定位 |
| `button.<device>_ring` | 让手机响铃（仅自有手机、30 秒冷却、需设备在线） |
| `sensor.<device>_last_location` | 状态 = 地址；未配置地址源时才是 `纬度, 经度` |
| `sensor.<device>_battery` | 电量（诊断类） |
| `binary_sensor.<device>_charging` | 充电状态（诊断类） |
| `sensor.<device>_last_locate_status` | 上次定位状态：成功 / 过期 / 未知（诊断类） |
| `device_tracker.<device>` | GPS 追踪器，可绑定 `person` |
| 服务 `huawei_find.refresh` | 同「全部刷新」，等全部设备查完才返回 |
| 服务 `huawei_find.set_cookie` | 直接更新会话 Cookie，无需操作界面（多账号时需指定目标账号） |

定位传感器的属性里还有：`latitude`、`longitude`、`accuracy`、`coordinate_system`、`address`、`address_stale`、`device_time`、`located_at`、`last_attempt`、`status`、`last_error`。

「全部刷新」按钮的属性里有：设备数、正在刷新的设备、各设备最近状态、当前 Cookie 已使用天数、最近一次会话保活是否成功及时间。

### 配置项

设置 → 设备与服务 → Huawei Cloud Find Device → **配置**：

| 选项 | 说明 |
|---|---|
| 地址来源 | `Auto`（推荐，华为 → 高德 → OSM 依次降级）/ `Huawei Map`（免 Key）/ `AMap / Gaode`（需 [高德开放平台](https://lbs.amap.com/) Key）/ `OpenStreetMap`（免 Key）/ `Disabled`（只用坐标） |
| 高德 Key | 仅选择高德时需要 |
| 自动刷新间隔 | 分钟，`0` = 关闭（默认）；最大 720 |

修改后立即生效，不会中段刷新、也不会让实体短暂变成不可用。

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

### 多账号

1. 再执行一次「设置 → 设备与服务 → 添加集成 → Huawei Cloud Find Device」；
2. 粘贴另一个账号的 Cookie；
3. 勾选要跟踪的设备（列表里会标注 `own` / `shared`，多账号时建议只选需要的那几台，避免重复实体）。

账号之间完全独立：各自的设备、刷新、保活与到期提醒互不影响；条目名称会带账号尾号以便区分（如 `Huawei Cloud Find Device (5311)`）。

调用 `huawei_find.set_cookie` 时，如果配置了多个账号，需要额外指定目标账号，避免写错。

### 已知限制

- 会话会过期（实测持续查询时约可用一周，没有自动续期），失效后需重新粘贴 Cookie，集成会弹通知提醒；
- 响铃只支持本账号自己的手机；家人共享的设备、手表/手环都不支持；
- 手表/手环常年离线，查询返回「设备不活跃」属于正常现象；
- 返回的坐标可能是云端缓存，无法区分「刚刚上报」与「上次缓存」；
- 地址反查依赖第三方服务，偶发超时会沿用上次地址并标记为已过期。

### Roadmap

- [x] 按钮触发 / 定位传感器 / GPS 追踪器
- [x] 地址反查（自动降级：华为 → 高德 → OSM）
- [x] 自动刷新间隔
- [x] 失败保留最后坐标、跳过禁用设备
- [x] 会话保活、失效提醒、到期提醒
- [x] 响铃按钮（仅自有手机）
- [x] 多账号支持
- [x] 选项即时生效、切换地址源立即重新解析
- [ ] 自动刷新的时间窗（晚间不查询）
- [ ] 设备列表热更新（不重载集成）
- [ ] 诊断信息导出（自动脱敏）

---

## English

### Why

The HA Companion app often reports stale locations on Huawei phones because of
aggressive battery management. Querying *Find Device* on the Huawei Cloud portal wakes
the phone's location service. This integration turns that into a HA button and reads the
resulting position.

### Features

- One "refresh all" button plus a per-device "refresh location" button (own and shared devices);
- A "ring device" button per own phone (max-volume alarm, 30 s cooldown);
- Location sensor showing the address (or coordinates when no address source is set);
- Separate battery, charging and last-locate-status entities;
- GPS `device_tracker` per device, ready to attach to a `person`;
- Optional auto refresh interval;
- Address lookup with automatic fallback (Huawei → AMap → OSM); switching sources re-resolves
  known coordinates immediately without waking the phone;
- Option changes apply instantly, no restart or reload;
- Failed lookups keep the last known position (marked stale) and results survive restarts;
- Session status is visible on the "refresh all" button, with expiry reminders and a
  re-auth notification;
- Devices disabled in the UI are skipped automatically;
- Multiple Huawei accounts are supported, fully isolated from each other.

### Installation

HACS → Integrations → ⋮ → Custom repositories → add this repo URL, category
*Integration*. Or copy `custom_components/huawei_find` into your HA config and restart.

Requirements: Home Assistant 2024.6+, no extra dependencies.

### Configuration

Copy the `Cookie` request header (DevTools → Network → any find-device request) and paste
it into the integration setup, then pick the devices to track. Add the integration again
to configure a second account.

### Entities

`button.*_refresh_all`, `button.*_refresh`, `button.*_ring`, `sensor.*_last_location`,
`sensor.*_battery`, `binary_sensor.*_charging`, `sensor.*_last_locate_status`,
`device_tracker.*`, plus the `huawei_find.refresh` and `huawei_find.set_cookie` services.

### Known limitations

- The session expires eventually (about a week with regular queries) and there is no
  automatic renewal; re-authenticate when notified;
- Ringing only works for phones on your own account, not for shared devices or wearables;
- Watches/bands are usually offline and report "device inactive";
- Returned coordinates may come from Huawei's cache;
- Address lookup is best effort and falls back to the previous address.

## License

[MIT](LICENSE)

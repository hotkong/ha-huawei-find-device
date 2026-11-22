# assets/screenshots —— README 截图

> 已收录 `device-page.png`（首屏）与 `config-flow.png`（配置章节），均已打码。
> `dashboard-map.png` / `refresh.gif` 仍待补，README 暂未引用。

## 一、图片清单

| 文件名 | 内容 | 状态 |
|---|---|---|
| `device-page.png` | 设备页实体列表（控制 / 传感器 / 诊断）：刷新、响铃、电量、网络、定位、上次定位状态 | ✅ 已收录（448×928 窄幅） |
| `config-flow.png` | 添加集成首屏「连接华为云」三选一菜单（粘贴 Cookie / 用浏览器获取 / 华为账号密码） | ✅ 已收录 |
| `dashboard-map.png` | `device_tracker` 绑定 `person` 后，HA 原生地图上的效果 | 待补 |
| `refresh.gif` | 点「刷新设备信息」→ 定位状态从 `stale`/`unknown` 变 `ok`，8–15 秒 | 待补 |

README 首屏用 `device-page.png`，「配置」章节用 `config-flow.png`；补地图/GIF 时按同样方式插入，总数控制在 4 张以内。

## 二、拍摄步骤（HA 2026.11 实测路径）

1. **设备页**：设置 → 设备与服务 → 设备 → 点选设备；裁掉左侧栏，保留实体列表与设备信息；
2. **配置流**：设置 → 设备与服务 → 添加集成 → 搜 “Huawei Cloud Find Device” → 点卡片进入集成页 →
   点「添加服务」，弹出三选一菜单；**截完直接关闭弹窗，不要提交**（提交会创建新条目）；
3. **地图**：把 `device_tracker` 绑到 `person` 后打开地图，缩放到能看出区域、但不暴露门牌；
4. **GIF**：在设备页点「刷新设备信息」，录屏裁剪到状态变化为止，8–15 秒。

## 三、打码清单（必须）

- 家庭地址、经纬度、区域名；
- SSID、手机号；
- 账号尾号（如 `(5311)`）、`user_id`；
- 设备序列号（`serial_number`）、家庭成员姓名、头像；
- 与集成无关的其他实体（裁掉，或另开一个空白仪表盘截）。

打码用图片编辑器涂**实心色块**，不要用模糊或马赛克——模糊仍可能被还原。

## 四、规格

- 常规截图宽 1200–1600 px（2x 截屏，页面按 46–50% 显示）；设备页实体列表可裁成窄幅（当前 448×928，按 34% 显示）；PNG 或 WebP，单张 < 300 KB；
- GIF < 5 MB、≤ 15 秒、≤ 720 px 宽；
- 命名：小写 + 连字符，固定用上面四个文件名；
- 中英双语只插一次图（图片不依赖界面语言），`alt` 里中英各一句。

## 五、更新与自检

1. 截新图时按上面 §二 的路径与 §三 的清单打码；
2. 覆盖本目录对应文件，`device-page.png` 已插入 README 首屏，`config-flow.png` 在「配置」章节；
3. 预览确认没有地址 / 经纬度 / 序列号 / 账号尾号 / SSID / 家人姓名；
4. 补 `dashboard-map.png` / `refresh.gif` 时按同样方式插入，总数控制在 4 张以内；
5. 可选：把 `device-page.png` 设为仓库 Social preview（Settings → General）；
6. 提交信息示例：`docs: update README screenshots`。

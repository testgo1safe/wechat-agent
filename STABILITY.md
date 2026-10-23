# 微信 Hook 稳定性说明

## 已知崩溃原因

| 操作 | 风险 | 现状 |
|------|------|------|
| Hook `/Decode_Pic` 调内部解密 | **必崩** | 已移除 |
| `SendImgMsg` 传假路径/wxid | **高** | C++ 已校验，空参数不调用微信 |
| 并发/高频 `SendText`/`SendImg` | **高** | Python 串行 + 4s 限速；C++ 互斥锁 |
| `test_image.py` 用 x/x 探测路由 | **必崩** | 已修复 |
| `find_image_key.py` 读内存 | 低 | 只读，不注入 |

## 日常守则

1. **收图解密只用 Python**：`find_image_key.py` + `dat_decrypt`，不要调 Hook 解密。
2. **测试发图**：`python test_image.py filehelper --send`（默认不测发图）。
3. **一次只跑一个 autoreply**：`autoreply_service.ps1 status` 确认无重复 PID。
4. **微信崩溃后**：重新登录 → 等 Hook 就绪 → `autoreply_service.ps1 restart`。
5. **更新 version.dll 前**：完全退出微信，再 `build_hook.ps1`。
6. **识图未开时**：收到图片会跳过并标记已读，不会每 3 秒重复刷日志。
7. **autoreply 已改为**：仅会话有变化时才查库；新消息到达后冷却 3 秒再读/发；每轮最多处理 1 条回复。
8. **禁止多个 autoreply**：`autoreply.pid` 单实例锁；`stop` 后再 `start`。
9. **Hook 跨进程互斥**：autoreply 与网页面板不能同时猛查 Hook（`wechat_hook_global.lock`）。
10. **真实健康检查**：`autoreply_service.ps1 status` 会显示 Hook、session、主消息库和备用 FTS 状态；不能只凭 PID 判断可用。
11. **自动拉起**：`autoreply_daemon.bat` 在 Python 意外退出后等待 5 秒重启；正常 `stop` 会写停止标记，不会被守护器再次拉起。
12. **FTS 分片隔离**：单个 `message_fts_v4_*` 异常时只隔离该分片，其余分片和明确未读的 session 摘要继续工作。
13. **主链路优先**：Hook + session + `message_N.db` 正常即为健康；FTS 未打开只标记为备用索引不可用，不再把完整主链路误报成降级。
14. **变化驱动轮询**：健康时约每 6 秒只查轻量 `SessionTable`，仅时间戳变化后读取具体消息；白名单具体消息兜底约每 60 秒轮转一次，避免空闲时每 3 秒持续打 Hook。
15. **禁用窗口轻触**：不再用 `ShowWindow` 做空闲保活；它不能可靠维持数据库句柄，还会额外触碰微信 UI 线程。
16. **活跃群单查入队**：每轮最多对一个变化群执行一次增量 SQL；模型可一次理解多条/多图，但最终 Hook 发送仍全局串行。
17. **群任务可恢复且会过期**：发现消息和推进游标在本地 SQLite 同事务提交；进程异常退出会恢复未发送批次，超过默认 120 秒则不补发旧话。

## 更新 DLL 后

```powershell
# 1. 退出微信（任务管理器确认无 Weixin.exe）
# 2. 编译部署
powershell -File E:\AIagent\build_hook.ps1
# 3. 登录微信
# 4. 启动服务
powershell -File E:\AIagent\start_all.ps1 -NoBrowser
```

## Hook 不可用时的表现

- 日志出现 `10061` / `积极拒绝` → 微信崩溃或未登录
- 日志出现 `get database handle which named session.db failed` → 微信当前未打开该库（后台释放常见），**不是损坏**。autoreply 改扫 FTS，不长时间重试 session.db。
- 日志出现 `sqlite3_prepare failed` / `HTTP Error 500` / `Hook 读库断路` → 库压力过大或异常，程序会**自动断路 60–120 秒停查**，降低微信弹「修复数据库」的概率。

## 微信弹「修复数据库」时

**根因（常见）**：Hook 与微信同时读/写同一套 SQLite（尤其 `message_*.db` / 巨大 `*-wal`），再叠加多实例 autoreply、长时间猛查已关闭的库，微信会认为本地库异常并提示修复。

**立刻做**：

1. `powershell -File E:\AIagent\autoreply_service.ps1 stop`（确认无残留 PID）
2. **不要**在弹窗期间强杀微信进程；先点修复或正常退出再登录
3. 修复/重登完成、聊天能正常显示后，再 `start`
4. 保持**只跑一个** autoreply；`interval` 建议 ≥ 3；白名单尽量小

**已做防护**：只查已打开的库、轮询走 FTS、session 低频探测、读库断路器、空闲保活不抢焦点且断路时不保活。

## 仍可能崩溃的情况（Hook 固有风险）

- 微信版本与 `global.h` 偏移不匹配（大版本升级后）
- 短时间内大量自动回复（建议 `interval` ≥ 3，`reply_to` 白名单尽量小）
- `image_gen` 发图比纯文本风险更高，不需要时可关
- 微信本身把旧 `message_N.db` 挪进 `.factory` 后 UI 空白：属客户端库轮换/显示问题，先修复数据库或重登，不要用外部工具硬改 db 文件

# emulator-control-tool

HarmonyOS 折叠屏模拟器折叠 / 悬停 / 旋转控制工具。

包含三部分：
- `emulator-control-server.py` -- 宿主机 HTTP 服务，执行 emulator 折叠命令
- `FoldTrigger.ets` -- ohosTest 测试侧封装，用例里直接调用
- `config.py` -- 集中配置（实例名/端口/窗口模式等）

链路：`用例 -> FoldTrigger.ets -> hdc rport -> emulator-control-server.py -> emulator`

---

## 环境要求

- Python 3.6+（纯标准库，无需 pip）
- DevEco Studio + 折叠屏模拟器（hdc 已连接）
- 自动探测 emulator / hdc 路径，无需配置

---

## 快速开始

三步：改配置 -> 跑服务 -> 跑用例。

### 第 1 步：改 config.py（告诉它控制哪台模拟器）

打开 `config.py`，把 `EMULATOR_INSTANCE` 改成你要的实例名（用 `emulator -list` 查可用名字）：

```python
EMULATOR_INSTANCE = "Pura X Max"   # 改成你的实例名
HEADLESS = False                   # False=带窗口看动画，True=无窗口(省资源)
PORT = 8766                        # 一般不用改
```

> 不改也行，默认是 `Mate X7`。改完**重启** `emulator-control-server.py` 生效。
> 只想临时换一台？不用改 config，直接 `python3 emulator-control-server.py "Pura X Max"` 命令行传参即可。

### 第 2 步：跑 emulator-control-server（手动启动）

```bash
python3 emulator-control-server.py
```

它会：(1) 自动拉起模拟器（没在跑的话）(2) 等 hdc 识别设备 (3) 建立 hdc 端口转发 (4) 监听 8766 等用例请求。**保持运行**，跑用例期间别关。

看到这两行就是成功了：

```
[OK] 目标设备（实例自动定位）: 127.0.0.1:5557
[OK] hdc 反向端口转发已建立（rport: 模拟器内 127.0.0.1:8765 -> 宿主机:8766）
```

按 `Ctrl+C` 停止（会自动清理端口转发 + 残留进程）。排查看 `emulator-control-server.log`。

### 第 3 步：跑用例

把 `FoldTrigger.ets` 放进测试工程 `ohosTest/ets/util/`，用例里调用：

```typescript
import { triggerFold, triggerLandscapeHover, sleep } from '../util/FoldTrigger';

// 展开（内屏大屏）
await triggerFold('open', 3000);

// 折叠（外屏小屏，必然竖屏）
await triggerFold('close', 4000);

// 悬停（半折，折痕可见）
await triggerFold('half-open', 3000);

// 悬停态校正到横屏（半折后方向不定，需要时调用）
await triggerLandscapeHover(driver);

await sleep(1000);   // 等待布局稳定
```

第二个参数是命令返回后额外等待的毫秒数。

---

## 三种折叠态

| state | 含义 | 方向 |
|-------|------|------|
| `open` | 内屏展开（大屏） | 由当前方向决定 |
| `close` | 折叠（外屏小屏） | 必然竖屏 |
| `half-open` | 悬停（半折，折痕可见） | 方向不定，需 `triggerLandscapeHover` 校正 |

---

## 验证（可选）

服务运行时可直接 curl 测试：

```bash
curl "http://127.0.0.1:8766/health"               # 健康检查
curl "http://127.0.0.1:8766/fold?state=open"      # 展开
curl "http://127.0.0.1:8766/fold?state=close"     # 折叠
curl "http://127.0.0.1:8766/fold?state=half-open" # 悬停
```

---

## 常见问题

**找不到 emulator / hdc**：设置环境变量 `DEVECO_SDK_HOME`（SDK 路径）、`HDC_PATH`（hdc 路径），或把它们加入 PATH。

**triggerFold 连接失败**：确认 emulator-control-server.py 在运行、`hdc list target` 能看到模拟器。重启服务会重建端口转发。

**多开模拟器**：在 config.py 设 `EMULATOR_INSTANCE = "实例名"` 指定要控制哪一台。多设备同时在线时，服务会自动把实例名映射到对应的 connect-key（靠 Emulator 进程的监听端口），精确路由到目标设备，不会误连到另一台。

**多个设备同时连接（hdc 报 `need connect-key`）**：多设备在线时，`emulator-control-server` 自动用 `-t <connect-key>` 路由到目标设备，按以下优先级定位：
1. 自动映射：根据 `EMULATOR_INSTANCE` 实例名找到对应 connect-key（推荐，多开时只需改实例名）
2. 显式指定：环境变量 `HDC_CONNECT_KEY` 或 config.py 的 `HDC_CONNECT_KEY`（值为 `hdc list targets` 的 connect-key，如 `127.0.0.1:5555`）
3. 兜底：取第一台并警告

---

## 配置参考

所有可调项集中在 `config.py`（纯 Python 变量，直接改值），改完重启 `emulator-control-server.py` 生效。

**优先级**（高 -> 低）：`命令行参数  >  环境变量  >  config.py  >  代码默认值`

| 配置项 | 命令行 | 环境变量 | config.py 变量 | 默认 |
|--------|--------|----------|----------------|------|
| 模拟器实例名 | `python3 emulator-control-server.py "Pura X"` | `EMULATOR_INSTANCE` | `EMULATOR_INSTANCE` | `Mate X7` |
| 窗口模式 | -- | `FOLD_HEADLESS` | `HEADLESS` | 带窗口 |
| 启动超时 | -- | `FOLD_EMU_TIMEOUT` | `EMU_START_TIMEOUT` | `120` |
| 服务端口 | -- | -- | `PORT` | `8766` |
| 设备端口 | -- | -- | `DEVICE_PORT` | `8765` |
| 多设备 connect-key | -- | `HDC_CONNECT_KEY` | `HDC_CONNECT_KEY` | 自动 |
| Emulator 路径 | -- | `EMULATOR_PATH` | `EMULATOR_PATH` | 自动探测 |
| hdc 路径 | -- | `HDC_PATH` | `HDC_PATH` | 自动探测 |

> 个人临时换值不必改 config.py，用环境变量或命令行参数覆盖即可。

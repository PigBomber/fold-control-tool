# fold-control-tool

HarmonyOS 折叠屏模拟器折叠 / 悬停 / 旋转控制工具。

包含两部分：
- `fold-server.py` —— 宿主机 HTTP 服务，执行 emulator 折叠命令
- `FoldTrigger.ets` —— ohosTest 测试侧封装，用例里直接调用

链路：`用例 → FoldTrigger.ets → hdc rport → fold-server.py → emulator`

---

## 环境要求

- Python 3.6+（纯标准库，无需 pip）
- DevEco Studio + 折叠屏模拟器（hdc 已连接）
- 自动探测 emulator / hdc 路径，无需配置

---

## 使用步骤

### 1. 启动折叠屏模拟器

DevEco Studio 里启动折叠屏模拟器，确认 `hdc list target` 能看到。

### 2. 启动宿主机服务

```bash
python3 fold-server.py              # 自动探测实例
python3 fold-server.py "Mate X7"    # 指定实例名
```

保持窗口运行，不要关闭。

### 3. 测试侧调用

把 `FoldTrigger.ets` 放进测试工程的 `ohosTest/ets/util/` 下，用例里导入调用：

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

**triggerFold 连接失败**：确认 fold-server.py 在运行、`hdc list target` 能看到模拟器。重启服务会重建端口转发。

**多开模拟器**：用实例名指定 `python3 fold-server.py "实例名"`。

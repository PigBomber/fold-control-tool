import http from '@ohos.net.http';
import { Driver } from '@kit.TestKit';

/**
 * 折叠/旋转触发工具
 *
 * 测试代码调用这些函数，通过 HTTP 请求宿主机的 fold-server.py，
 * 宿主机执行 Emulator 命令切换折叠/旋转状态，返回后继续执行用例。
 *
 * 通用连接机制（所有平台通用，无需配置 IP）：
 *   使用 hdc rport 反向端口转发，模拟器内访问 127.0.0.1:8765
 *   通过 rport 转发到宿主机的 8766（fold-server 监听端口）。
 *
 * 使用前必须：
 *   1. 启动宿主机服务：python3 scripts/fold-server.py
 *   2. 服务会自动建立 hdc rport 转发
 */

// 通过 hdc fport 转发，模拟器内 127.0.0.1 = 宿主机（所有平台通用）
const FOLD_SERVER_HOST = '127.0.0.1';
const FOLD_SERVER_PORT = 8765;

export type FoldState = 'open' | 'half-open' | 'close';
export type RotationDirection = 'left' | 'right';

/**
 * 发送请求到 fold-server，同步等待结果
 */
async function sendCommand(path: string): Promise<void> {
  const httpRequest = http.createHttp();
  const url = `http://${FOLD_SERVER_HOST}:${FOLD_SERVER_PORT}${path}`;
  try {
    const response = await httpRequest.request(url, {
      method: http.RequestMethod.GET,
      connectTimeout: 5000,
      readTimeout: 10000,
    });
    if (response.responseCode !== 200) {
      throw new Error(`fold-server 返回错误码: ${response.responseCode}`);
    }
    const body = JSON.parse(response.result as string) as Record<string, Object>;
    if (body['success'] !== true) {
      throw new Error(`命令失败: ${body['message'] ?? '未知错误'}`);
    }
  } catch (e) {
    throw new Error(
      `无法连接 fold-server（${url}）。\n` +
      `请检查：\n` +
      `1. 宿主机已启动：python3 scripts/fold-server.py\n` +
      `2. 已建立端口转发：hdc fport tcp:8765 tcp:8765\n` +
      `3. 模拟器已连接：hdc list target\n` +
      `原始错误: ${(e as Error).message ?? e}`
    );
  } finally {
    httpRequest.destroy();
  }
}

/**
 * 判断当前是否横屏（宽 > 高）
 * 使用 UiTest Driver 原生 API，不依赖 @kit.ArkUI display 模块
 */
async function isLandscape(driver: Driver): Promise<boolean> {
  const size = await driver.getDisplaySize();
  return size.x > size.y;
}

/**
 * 触发折叠状态切换（同步等待结果）
 * @param state 目标状态：open（展开）/ half-open（半折/悬停）/ close（折叠）
 * @param waitAfter 命令返回后额外等待的毫秒数（默认 3000）
 */
export async function triggerFold(state: FoldState, waitAfter: number = 3000): Promise<void> {
  await sendCommand(`/fold?state=${state}`);
  await sleep(waitAfter);
}

/**
 * 触发旋转（同步等待结果）
 * @param direction 方向：left / right
 * @param waitAfter 命令返回后额外等待的毫秒数（默认 3000）
 */
export async function triggerRotation(direction: RotationDirection, waitAfter: number = 3000): Promise<void> {
  await sendCommand(`/rotation?direction=${direction}`);
  await sleep(waitAfter);
}

/**
 * 切换到横屏悬停态（HoverMode 专用）
 *
 * 兼容逻辑：
 *   1. 先切到悬停态（half-open）
 *   2. 检查是否横屏（宽 > 高，折痕水平）
 *   3. 若竖屏（折痕垂直），旋转到横屏
 *
 * @param driver UiTest Driver 实例（用于读取屏幕方向）
 */
export async function triggerLandscapeHover(driver: Driver): Promise<void> {
  // 先切到悬停态
  await triggerFold('half-open', 2000);

  // 悬停后检查是否横屏，不是则旋转
  if (!await isLandscape(driver)) {
    await triggerRotation('right', 2000);
  }
}

/**
 * 等待指定毫秒
 */
export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

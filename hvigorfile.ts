import { appTasks } from '@ohos/hvigor-ohos-plugin';
import { execSync, spawn } from 'child_process';
import * as path from 'path';
import * as fs from 'fs';

/**
 * 确保 fold-server 已启动（hvigor 加载时自动调用）。
 * 检测端口 8766 健康检查，未运行则以 detached 后台进程启动 fold-server.py。
 * 构建结束后进程继续存活，测试结束手动 Ctrl+C 或下次重启自动复用。
 */
function ensureFoldServer(): void {
  const PORT = 8766;
  const scriptPath = path.join(__dirname, 'fold-server.py');
  const logPath = path.join(__dirname, 'fold-server.log');

  // 健康检查（Node http，不依赖 curl，跨平台）
  const isHealthy = (): boolean => {
    try {
      const result = execSync(
        `node -e "require('http').get('http://127.0.0.1:${PORT}/health',r=>{process.exit(r.statusCode===200?0:1)}).on('error',()=>process.exit(1))"`,
        { stdio: 'ignore', timeout: 3000 }
      );
      return true;
    } catch {
      return false;
    }
  };

  // 已运行则跳过
  if (isHealthy()) {
    console.log('[fold-server] 已在运行 (端口 8766 健康检查通过)，跳过自动启动');
    return;
  }

  // 脚本不存在则跳过（非测试场景）
  try {
    if (!fs.existsSync(scriptPath)) {
      return;
    }
  } catch {
    return;
  }

  // 以 detached 后台进程启动，stdout/stderr 落盘到日志文件
  // Python 侧也会自行写日志（双保险：Python 重定向 stdout + spawn fd）
  const pythonBin = process.platform === 'win32' ? 'python' : 'python3';
  let logFd: number | undefined;
  try {
    logFd = fs.openSync(logPath, 'a');
  } catch {
    // 日志文件不可写不影响主流程
  }
  const child = spawn(pythonBin, [scriptPath], {
    detached: true,
    stdio: logFd !== undefined ? ['ignore', logFd, logFd] : 'ignore',
    cwd: __dirname
  });
  child.unref();
  console.log(`[fold-server] 后台启动中 (PID ${child.pid})，日志: fold-server.log`);

  // 等待 HTTP 服务就绪（最多重试 10 次 × 1s，不会超时因为 HTTP 已先于 setup_fport 启动）
  let retries = 10;
  const checkReady = (): void => {
    if (isHealthy()) {
      console.log('[fold-server] ✓ HTTP 服务已就绪');

      // 验证 hdc rport 是否已建立（设备端测试依赖此转发连通 fold-server）
      try {
        const rportOutput = execSync('hdc fport ls', { encoding: 'utf8', timeout: 3000 });
        if (rportOutput.includes('8765') && rportOutput.includes('8766')) {
          console.log('[fold-server] ✓ hdc rport (8765→8766) 已建立');
        } else {
          console.warn('[fold-server] ⚠ hdc rport (8765→8766) 未建立 — 设备端测试可能无法连接 fold-server');
          console.warn('[fold-server]   请确认模拟器已连接: hdc list target');
        }
      } catch {
        console.warn('[fold-server] ⚠ 无法验证 hdc rport（hdc 不可用，跳过）');
      }
      return;
    }
    if (retries-- > 0) {
      setTimeout(checkReady, 1000);
    } else {
      console.warn('[fold-server] ✗ 启动超时，请查看日志: fold-server.log');
    }
  };
  setTimeout(checkReady, 1500);
}

// hvigor 加载时自动确保 fold-server 就绪
ensureFoldServer();

export default {
    system: appTasks,  /* Built-in plugin of Hvigor. It cannot be modified. */
    plugins:[]         /* Custom plugin to extend the functionality of Hvigor. */
}

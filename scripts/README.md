# 脚本

命令都在仓库根目录执行。Windows 脚本使用 PowerShell 7（`pwsh`）。

## 启动

| 平台 | 入口 |
| --- | --- |
| Windows | `pwsh -File scripts/start-local.ps1`，或双击 `scripts/start-local.cmd` |
| Linux / macOS | `sh scripts/start-local.sh` |

源码启动会准备 Python 依赖、构建前端，然后启动服务。

## 打包

| 产物 | 命令 |
| --- | --- |
| Windows 安装版与便携版 | `pwsh -File scripts/build-windows.ps1`、`pwsh -File scripts/build-windows-distributions.ps1` |
| 飞牛 fnOS 包（Windows） | `pwsh -File scripts/build-fnos.ps1 -Platform x86` 或 `-Platform arm` |
| 飞牛 fnOS 包（Linux） | `bash scripts/build-fnos.sh x86` 或 `bash scripts/build-fnos.sh arm` |

fnOS 打包的实际实现是 `scripts/build-fnos.sh`：校验 manifest 的平台、Python ABI 与版本号一致，构建前端，按平台拉取 Linux 依赖，再用 `fnpack` 生成 `.fpk`。`build-fnos.ps1` 是薄封装，在 Windows 上找到 `bash.exe`（PATH 或 Git 默认安装路径）后调用它，因此需要 Git for Windows 或 WSL，并要求 `fnpack`、`npm` 在 PATH 中。

## 运维与工具

| 用途 | 入口 |
| --- | --- |
| Windows 防火墙配置 | 管理员终端运行 `pwsh -File scripts/configure-windows-firewall.ps1` |
| 更新 FnDepot 应用源 | `pwsh -File scripts/update-fndepot.ps1`，参数见 [应用源说明](../docs/fndepot.md) |
| 测量音频流交付间隔 | `python scripts/measure_stream.py <STREAM_URL>` |

防火墙工具默认绑定仓库虚拟环境的 Python，也可以用 `-Program` 指定 MiCast 可执行文件；它只允许专用网络内本地子网的入站 TCP/UDP，不固定运行时端口。

## 容器内脚本

- `start.sh`：Docker 主镜像的启动入口。
- `receiver-callback.sh`：发送带进程身份与事件序号的接收器回调。
- `receiver-session-start.sh`、`receiver-session-stop.sh`、`receiver-volume.sh`：Docker AirPlay 2 接收器容器调用，用于会话状态与音量回传。

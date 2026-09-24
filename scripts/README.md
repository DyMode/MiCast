# 脚本

从仓库根目录执行下列命令。Windows 脚本使用 PowerShell 7（`pwsh`）。

| 用途 | 入口 |
| --- | --- |
| Windows 源码启动 | `pwsh -File scripts/start-local.ps1`，或双击 `scripts/start-local.cmd` |
| Linux 源码启动 | `sh scripts/start-local.sh` |
| Windows 打包 | `pwsh -File scripts/build-windows.ps1` |
| Windows 构建 fnOS x86 / ARM 包 | `pwsh -File scripts/build-fnos.ps1 -Platform x86` / `-Platform arm` |
| Linux 构建 fnOS x86 / ARM 包 | `bash scripts/build-fnos.sh x86` / `bash scripts/build-fnos.sh arm` |

fnOS 打包的真正实现是 `scripts/build-fnos.sh`（bash）：它会校验 manifest 的 Python ABI 与 `micast/__init__.py` 的 `__version__` 同 manifest `version=` 一致，构建前端，按平台拉取 Linux 依赖并用 fnpack 生成 `.fpk`。`build-fnos.ps1` 只是薄封装——在 Windows 上找到 `bash.exe`（PATH 或 Git 默认安装路径）后透传 `-Platform` 参数调用 sh 脚本，因此需要已安装 Git for Windows 或 WSL，且 `fnpack` 与 `npm` 需在 PATH 中。
| 生成应用商店宣传图尺寸 | `python scripts/build-product-images.py`（默认把 `docs/screenshots/*.png` 转为 1780×1004） |
| 重新生成商店简介图 | 简介引用 `docs/screenshots/*.png`（780 宽 UI 裁切版，fnOS 应用中心按图片原始像素渲染，只能用物理尺寸控制）；高清产品图存档在 `docs/screenshots/full/`，可由 `python scripts/build-product-images.py docs/screenshots/full --width 780 --height 642 --out-dir ...` 重新裁切 |
| 测量音频流交付间隔 | `python scripts/measure_stream.py STREAM_URL` |
| Windows 防火墙配置 | 管理员终端运行 `pwsh -File scripts/configure-windows-firewall.ps1` |

源码启动会准备依赖并构建前端。防火墙工具默认绑定仓库虚拟环境的 Python，也可通过 `-Program` 指定 MiCast 可执行文件；只允许专用网络内本地子网的入站 TCP/UDP，不固定运行时端口。

`start.sh` 由 Docker 主镜像调用。`receiver-session-start.sh`、`receiver-session-stop.sh` 和 `receiver-volume.sh` 由 Docker AirPlay 2 接收器调用，用于会话和音量回传。

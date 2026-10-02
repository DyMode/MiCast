# 部署说明

## Windows

安装版提供 `MiCast.exe`，便携版是同名压缩包；两者都内含 Web 管理界面、经典 AirPlay 与 DLNA 接收。

- 首次运行请允许 Windows 防火墙的**专用网络**访问，mDNS、SSDP 与音频入口才能在局域网可见。
- 数据位置：安装版设置存 `%APPDATA%\MiCast`，日志与运行文件存 `%LOCALAPPDATA%\MiCast`；便携版（含 `portable.flag`）全部存在程序目录旁的 `data\`。
- 直接从源码运行时使用 `%APPDATA%\MiCast-Dev`。

从源码构建安装版与便携版（安装版需要 Inno Setup 6）：

```powershell
pwsh -NoProfile -File scripts/build-windows-distributions.ps1
```

## 飞牛 fnOS 原生包

在应用中心搜索安装，或下载 `.fpk` 手动安装（x86 与 ARM64 均有）。

- 依赖应用中心的 **Python 3.12**；管理界面走 fnOS 统一网关 `/app/micast/`，不占用固定管理端口。
- 经典 AirPlay 与 DLNA 开箱可用。AirPlay 2 为**单入口、实验性**，安装向导可选择启用，也可在首次引导或设置中调整；ARM 包不含 AirPlay 2 运行时，开关会自动隐藏。
- 卸载时可选择本地数据的处理方式，默认**保留设备配置、删除米家登录资料与令牌**。

构建：

```powershell
pwsh -NoProfile -File scripts/build-fnos.ps1            # x86
pwsh -NoProfile -File scripts/build-fnos.ps1 -Platform arm
```

产物输出到 `dist/fnos/`。包结构与生命周期见 [fnOS 原生包说明](../packaging/fnos/README.md)。

## Docker

经典版为单容器，必须使用 **host 网络**，mDNS、SSDP 与 AirPlay 端口才能参与局域网发现：

```bash
docker build -f docker/Dockerfile -t micast:latest .
docker run -d --name micast --restart unless-stopped \
  --network host \
  -e MICAST_DATA_DIR=/data \
  -v "$(pwd)/data:/data" \
  micast:latest
```

`docker-compose.classic.yml` 是同一镜像的本地编排入口：

```bash
cd docker
docker compose -f docker-compose.classic.yml up -d --build
```

另外两种编排都是**实验性**，不属于默认发行方式：

- `docker-compose.single.yml`：控制器 + 一个固定 AirPlay 2 接收器，PCM 走内网，接收器用 macvlan 参与发现。使用前复制并填写 `docker/.env`（`MICAST_LAN_*` 必须与实际网卡和局域网一致）。
- `docker/experimental/docker-compose.multi.yml`：多实例 AirPlay 2 编排，需要 macvlan、宿主的 Docker Socket、独立 LAN 地址段与人工网络配置。

全部文件、环境变量与启动命令见 [docker/README.md](../docker/README.md)。

## 网络要求

NAS（或运行 MiCast 的电脑）与手机、音箱必须处于可互相访问的同一局域网：

- mDNS（组播 5353）与 SSDP（1900）不能被访客网络隔离或交换机拦截，否则入口或音箱发现不了。
- 音频入口端口由运行时分配：经典 AirPlay 的 RTSP 默认从 42500 起、每个会话再占 3 个 UDP 端口（默认从 42600 起），当前 AirPlay 2 接收器固定使用 TCP 7000；音频流默认 42400，管理页面默认 42300（fnOS 原生包走统一网关）。

## 数据、备份与迁移

主要持久化配置文件：

| 文件 | 内容 |
| --- | --- |
| `micast.json` | 全部设置（音箱、入口、组合、EQ、延迟、音频参数） |
| `access.json` | 管理页面的访问账号 |
| `xiaomi-account.json` | 米家账号资料 |
| `xiaomi-tokens.enc` | 加密后的小米登录令牌 |

它们随存储方式放在不同位置：

| 存储方式 | 数据目录 |
| --- | --- |
| 便携版（存在 `portable.flag` 或 `MICAST_PORTABLE=1`） | 程序目录旁的 `data/` |
| Windows 安装版 | `%APPDATA%\MiCast` |
| Windows 源码运行 | `%APPDATA%\MiCast-Dev` |
| Linux / macOS 源码运行 | `~/.local/share/micast`、`~/Library/Application Support/MiCast` |
| 飞牛 fnOS 原生包 | fnOS 为该应用分配的持久化目录（`$TRIM_PKGVAR`） |
| Docker / 编排版 | `MICAST_DATA_DIR` 指向的挂载目录 |

迁移时注意：

- `micast.json`、`access.json`、`xiaomi-account.json` 是纯文本，可以直接复制到另一台机器或另一种存储方式的数据目录。
- `xiaomi-tokens.enc` 的加密密钥派生自机器特征（首选网卡 MAC，Linux 回退到 `/etc/machine-id`）。换机、换网卡或 machine-id 变化后该文件无法解密，界面会给出重新扫码登录的入口；其余设置不受影响。需要跨机迁移令牌时，可用 `MICAST_ENCRYPTION_KEY` 固定加密密钥。
- fnOS 卸载默认保留设备配置（含管理访问设置），删除米家登录资料与令牌；需要完整保留或彻底删除时在卸载向导里选择。


## 统一安装预检与端口规划

源码与 Windows 可执行文件支持 --preflight；源码启动脚本在新建安装时预检，已有配置的启动保留管理恢复入口。飞牛安装向导默认 auto，允许手填端口；可选功能不足不阻断其他协议可用的安装。

在仓库根目录使用 python -m micast.docker_install --mode classic 规划宿主机端口；single/multi 替换 mode。默认只生成方案和启动命令，--up 才启动 Docker。经典容器允许自动避让并按实际端口检查健康；桥接映射必须在宿主机规划，不能在容器内自动改映射。

端口分配与冲突处理见 [端口配置](ports.md)。

fnOS 用户共享目录包含 `logs/`、`diagnostics/` 和 `backups/`，分别保存运行日志、导出的诊断报告和脱敏配置参考副本。登录凭据与完整升级备份保存在应用私有数据目录。

# MiCast fnOS 原生包

## 应用结构

- 架构：x86、ARM64（manifest 平台名为 `arm`）
- 依赖：应用中心 `python312`
- 公开入口：`/app/micast/`
- 网关 Socket：`${TRIM_APPDEST}/app.sock`
- 持久数据：`${TRIM_PKGVAR}`
- 运行身份：`micast`
- AirPlay 2：单入口；安装向导默认请求启用，运行条件不满足时单独受阻

应用不监听固定的管理端口，因此 manifest 使用 `checkport=false`，由 fnOS 统一网关转发到 Unix Socket。安装及升级向导展示用户协议和隐私政策，默认不勾选同意；脚本再次校验同意版本。无需选择投送协议或填写端口。全新安装默认请求启用可用投送功能，可调整端口自动分配；重装沿用已保留的配置。手动端口与协议开关统一放在应用设置中；AirPlay 2 接收器固定使用 TCP 7000，不能修改或自动顺延；安装前初检，安装回调按应用用户验证时钟运行时。至少一种输入协议可用即可安装。管理访问、米家登录和音箱选择由应用内引导完成；后续设置与功能恢复统一使用应用设置页。

## 生命周期

- 安装时检查 Python 3.12 与 AirPlay 2 运行时能力。
- 启动后通过 Unix Socket 健康检查确认服务就绪。
- 停止和卸载时清理 MiCast 私有的 AirPlay 2 子进程及 Socket。
- 卸载向导可选择仅保留设备配置、保留全部私有配置与登录或删除全部私有数据；默认保留设备配置并删除小米凭据、管理账号、通知密钥和账号记录。
- 所有卸载策略均不自动删除用户共享目录、另存文件和浏览器数据，向导明确提示单独处理。
- 启动时检查主日志，达到 5 MiB 时轮转并保留一份旧日志；不是运行期间的硬性大小上限。
- 升级备份原子写入，权限 0600；恢复仅补回缺失文件，损坏备份会保留并中止回调。
- 协议源文件为 `web/src/legal-documents.json`；修改后执行 `python scripts/sync-legal.py` 同步文档和向导，构建时校验一致性。
- 向导控件规则由 `scripts/check-fnos-package.py` 校验（构建前自动执行）：`checkbox`/`radio`/`select` 的 `initValue` 必须是字符串（未勾选写 `""`，已勾选写选项 `value`），`options` 非空，`text` 的 `initValue` 不超过 1024 字符，`required` 规则必须带 `message`。写成数组会触发 fnpack 的通用 JSON 报错。

逐阶段检查与实机验收清单见 [飞牛生命周期核查](../../docs/fnos-lifecycle-audit.md)。

## 构建

```powershell
pwsh -NoProfile -File scripts/build-fnos.ps1
```

ARM 包使用：

```powershell
pwsh -NoProfile -File scripts/build-fnos.ps1 -Platform arm
```

Linux 可分别运行 `bash scripts/build-fnos.sh x86` 或
`bash scripts/build-fnos.sh arm`。产物输出到 `dist/fnos/`。

构建前后有三道保障，避免"看起来成功其实没有产物"：

1. 打包前校验 `packaging/fnos` 与 staging 目录（`scripts/check-fnos-package.py`）；
2. vendor 依赖下载失败会自动重试（`MICAST_FNOS_VENDOR_ATTEMPTS`，默认 3 次）；
3. **fnpack 在内容校验失败时仍返回退出码 0**，因此构建脚本会检查其输出中的 `Packing successfully`、确认 `micast.fpk` 已生成且晚于本次打包开始时间，并校验最终产物存在——任一不满足即以非零退出，不再留下过期包。出现"Packing failed"时按提示修正包内容后重跑。

ARM 包暂不包含仅有 x86-64 构建的内置 AirPlay 2 运行时，因此会自动隐藏
AirPlay 2 开关；经典 AirPlay、DLNA 和其他功能不受影响。

# FnDepot 应用源

在支持 V2 外部源的 FnDepot 客户端（按其文档使用高于 0.0.7 的版本）中添加：

https://github.com/DyMode/MiCast

也可以添加 JSON 直链：

https://raw.githubusercontent.com/DyMode/MiCast/main/fnpack.json

同步后选择 MiCast，客户端会按设备架构选择 x86 或 ARM 安装包。安装依赖飞牛应用中心的 Python 3.12，最低系统版本由源中对应发布版本声明。

这是开发者维护的外部源，添加后在当前用户的 FnDepot 客户端生效。

## 发布新版本

待 GitHub Release 中两个架构的 FPK 上传完成后，使用 PowerShell 7 执行：

```powershell
pwsh -NoProfile -File scripts/update-fndepot.ps1
```

指定正式发布版本：

```powershell
pwsh -NoProfile -File scripts/update-fndepot.ps1 -Tag v0.3.2
```

脚本读取公开 Release 的安装包地址、大小和 SHA256，以及对应标签中的 manifest 和权限信息，保留已收录版本。检查生成的根目录 `fnpack.json` 后，将它提交并推送到默认分支。

不得静默替换同版本安装包；内容改变时发布新版本，并再次更新源。

格式依据：https://github.com/EWEDLCM/FnDepot

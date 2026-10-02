# 平台图标

使用 `.venv\Scripts\python.exe assets\export_icons.py` 生成平台图标，同步 Web 图标至 `web/public/icons`，并生成 `assets/micast-icons.zip`。

- Windows：透明圆角底板，多尺寸独立渲染，32 位 BGRA 图像与显式 AND 掩码。16–48 px 的外缘使用二值透明度，内部图形保留抗锯齿。
- iOS：180×180 不透明 RGB 图标，陶土红背景，由系统裁切圆角。
- fnOS：64×64 与 256×256 sRGB PNG，透明圆角，底板占画布 97%，打包使用同步至 Web 目录的图标。
- 浏览器：`favicon.svg` 与 `favicon.ico` 采用适合小尺寸的图形；页面内 `micast.svg` 使用品牌母版比例。

`fnos`、`windows`、`tray`、`web` 按用途组织导出文件，尺寸后缀表示像素大小。视觉母版见 [品牌图标](../brand-approved/README.md)。

构建 Windows 程序后运行 `.venv\Scripts\python.exe assets\verify_icons.py --exe`，检查程序资源、透明度掩码、系统绘制结果和触摸图标尺寸。

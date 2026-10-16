# 飞牛应用中心上架材料

## 介绍与新功能

应用介绍使用 `packaging/fnos/manifest` 的 `desc`，当前版本更新说明使用同文件的
`changelog`。简介讲清用途、功能与限制；更新说明只列当前版本用户能感知的变化，
不混入下载表格、构建过程或完整提交历史。

## 截图轮播

轮播由应用中心展示。上架时将以下六张原图作为独立截图素材按顺序提交，
不要再在 `desc` 中逐张插入 `<img>`，也不需要在 MiCast 前端实现轮播。
平台后台的具体字段和尺寸要求以提交界面为准。

| 顺序 | 文件 | 内容 |
| --- | --- | --- |
| 1 | `docs/screenshots/01-player.png` | 播放与歌曲信息 |
| 2 | `docs/screenshots/02-speakers.png` | 音箱与音量控制 |
| 3 | `docs/screenshots/03-eq.png` | 每台音箱独立调音 |
| 4 | `docs/screenshots/04-links.png` | 播放链路与诊断 |
| 5 | `docs/screenshots/05-stereo.png` | 多音箱与立体声组合 |
| 6 | `docs/screenshots/06-mobile.png` | 移动端操作 |

截图必须与送审版本的真实界面一致。此次仅整理配置与素材清单，未重拍或修改图片，
这些现有截图需在正式提交前核对是否覆盖当前界面变化。

FnDepot 使用独立的 `preview_urls`。`scripts/update-fndepot.ps1` 在正式 Release
发布后生成包含上述六张图的列表，并优先使用该版本 manifest 中的简短更新说明。
不要为尚未发布的包预填下载地址、哈希、大小或发布日期。

## 安装引导

安装页只说明安装后的连接步骤，无需填写端口或选择协议。全新安装自动分配可调整
端口并检测投送能力；重装读取保留的配置。协议开关和手动端口统一在应用设置中调整。
AirPlay 2 固定端口及其运行环境检查仍执行；部分协议不可用不会阻止其他可用协议安装。

参考：
- https://developer.fnnas.com/docs/quick-started/publish-application
- https://developer.fnnas.com/docs/core-concepts/manifest

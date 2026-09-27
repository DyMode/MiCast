# Docker 部署

三种编排，只有经典版是默认发行方式。全部命令都在仓库根目录执行。

## 经典版（单容器）

`docker/Dockerfile`，提供经典 AirPlay、DLNA、多音箱同步、立体声组合与 Web 管理界面。

```bash
docker build -f docker/Dockerfile -t micast:latest .
docker run -d --name micast --restart unless-stopped \
  --network host \
  -e MICAST_DATA_DIR=/data \
  -v "$(pwd)/data:/data" \
  micast:latest
```

编排方式：

```bash
cd docker
docker compose -f docker-compose.classic.yml up -d --build
```

必须使用 host 网络：mDNS、SSDP 与 AirPlay 音频端口要直接参与局域网发现与会话。

## AirPlay 2 单例版（实验性）

`docker-compose.single.yml`：控制器加一个固定的 AirPlay 2 接收器，不启动编排器，也不挂载 Docker Socket。两者通过内部网络传输 PCM，接收器用 macvlan 参与局域网发现。

```bash
cp docker/.env.example docker/.env
# 按实际网卡与局域网填写 MICAST_LAN_*
docker compose --env-file docker/.env -f docker/docker-compose.single.yml up -d --build
```

只提供一个 AirPlay 2 入口，没有新增实例的入口；首次打开 Web 引导页后启用。`MICAST_LAN_*` 必须与实际 NAS 网卡和局域网一致。

## 多实例编排（实验性）

`experimental/docker-compose.multi.yml` 保留多入口 AirPlay 2 编排方案，需要 macvlan、宿主的 Docker Socket、独立的 LAN 地址段与人工网络配置。

所有 macvlan 参数都必须在 `docker/.env` 中填写；地址段要避开路由器的 DHCP 池，并确认宿主机网卡名称正确。

## 数据目录

容器内数据目录由 `MICAST_DATA_DIR` 指定，挂载出来即可保留设置与登录态；文件清单与迁移注意事项见[部署说明](../docs/deployment.md)。

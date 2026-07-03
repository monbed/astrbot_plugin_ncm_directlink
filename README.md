# astrbot_plugin_ncm_directlink

搜索网易云音乐，按需**获取直链**或**直接发送音乐文件**的 AstrBot 插件。

使用 API：https://github.com/neteasecloudmusicapienhanced/api-enhanced （或其他 NeteaseCloudMusicApi 兼容实现）

## 使用

```
/下载音乐 <歌名>        # 支持带空格的完整歌名
→ 回复序号选择歌曲（60 秒内有效，回复 0 取消）
→ 回复发送方式：1. 直链  2. 文件（60 秒内有效，回复 0 取消）
```

- 序号回复只对**发起搜索的用户**生效，群内多人可同时各自点歌互不干扰。
- 选择「文件」时，插件按接口返回的实际格式（mp3/flac 等）下载，**先重命名为「歌名 - 歌手.格式」再发送**，发送后自动清理本地文件。

## 配置

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| `apiurl` | **必填**。自部署的网易云 API 服务地址，如 `http://127.0.0.1:3000` | 空 |
| `level` | 音质等级：`standard`/`higher`/`exhigh`/`lossless`/`hires`/`jyeffect`/`sky`/`jymaster` | jyeffect |
| `cookie` | 网易云账号 Cookie（含 `MUSIC_U=`）。VIP / 高音质歌曲需要 | 空 |
| `limit` | 搜索返回的歌曲数量 | 10 |
| `timeout` | 搜索/取链接口超时（秒）；文件下载固定 300 秒 | 10.0 |

## 注意事项

- 下载会员/高音质歌曲需要具有相应权益的网易云 Cookie。
- **文件发送**依赖消息平台的文件消息能力（QQ 需 NapCat / Lagrange 等协议端支持）。协议端与 AstrBot 不在同一台机器/容器时，请配置 AstrBot 的 `platform_settings.path_mapping` 做路径映射，否则协议端读不到本地文件。
- 无损/母带格式文件较大，发送耗时取决于协议端上传速度。

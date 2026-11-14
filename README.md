# AIRemote

从手机继续 Mac 上的 AI 对话。支持网页、Android APK 和 Mac 连接器应用，可部署到自己的服务器。

[下载最新版本](https://github.com/Winlifes/AIRemote/releases/latest) · [MIT License](LICENSE) · [安全说明](SECURITY.md)

## 功能

- 读取 Mac Codex 原有对话、真实运行状态、回复和关联文件；向同一个桌面对话发送指令。
- 原生项目层级，展开项目才读取列表；打开聊天才读取最新消息，上翻才读取更早历史。
- 原生队列添加、编辑、删除、排序、清空和立即执行，以及新建真实对话。
- DeepSeek Harness 现有 Host 的项目、聊天、续聊、文件、新建和队列；其当前版本没有排序接口。
- 手机抽屉侧栏、胶囊输入框、Markdown、用户消息右对齐和真实处理时间。
- 侧栏与全屏遮罩同时过渡；当前 Codex 模型、思考强度、工作/计划模式及权限从原桌面 owner 实时读取。
- Mac 应用启动/停止连接器；Android 支持系统文件下载和 GitHub Releases 更新。
- Codex 对话目标：查看状态、目标消耗与预算，创建/修改、暂停/继续及关闭；在原桌面 app-server 上写入并回读确认。

CodeBuddy / Claude、多台 Mac、图片和音频指令、手机处理 AI 审批仍待扩展。

## 架构

```text
Android / 手机浏览器 --HTTPS--> 自托管 relay <--HTTPS 主动轮询-- Mac agent
                                                                  |
                                                          loopback connector
                                                                  |
                                             原 Codex owner / DeepSeek Host
```

Mac 不需要公网 IP 或入站端口。已有聊天交给原桌面 owner 执行，不另开 SDK 聊天冒充接管。

## 部署中继

需要 Linux、Python 3.9+、Caddy 和已指向服务器的域名。示例域名需要替换。

```sh
git clone https://github.com/Winlifes/AIRemote.git
cd AIRemote
cd web && npm ci && npm run build && cd ..
python3 deploy/setup_relay.py --origin https://remote.example.com --config /your/private/relay.json
```

初始化只打印文件路径。`phone-login.txt` 保存手机登录信息，`mac-agent-credentials.json` 保存 Mac 独立凭据；均为 0600，不要上传仓库。

将 `relay/`、`deploy/`、`web/dist/`、`VERSION` 放到服务器 `/opt/airemote/releases/<version>/`，以 root 执行：

```sh
python3 /opt/airemote/releases/<version>/deploy/install.py \
  --release /opt/airemote/releases/<version> \
  --config /your/private/relay.json --domain remote.example.com --auto-update
```

安装器创建独立用户与 systemd 服务，服务仅监听 loopback。Caddy 负责 HTTPS，保留已有其他站点。

`--auto-update` 启用每六小时的 GitHub 稳定版更新。可省略并手动执行 `python3 /opt/airemote/current/deploy/update_server.py --apply`。只检查使用 `--apply` 以外的默认模式。更新先校验 SHA-256，再切换独立版本目录；服务未通过版本健康检查时恢复上一个版本，配置和数据库始终保留。`systemctl disable --now airemote-update.timer` 可关闭自动更新。

## 配置 Mac

Mac 须保持联网，对应 AI 客户端保持运行和登录。准备 Python 3.9+；Apple 的 Python 可能需要先安装 Command Line Tools。

将上一步的私有 `mac-agent-credentials.json` 安全复制到 Mac，然后：

```sh
python3 deploy/configure_mac.py \
  --credentials /your/private/mac-agent-credentials.json \
  --workspace /your/project
```

默认配置位于 `~/Library/Application Support/AIRemote/.local/`。可重复 `--workspace` 授权多个目录，`--home` 指定配置位置。默认接入原生项目目录；文件下载仍限制在授权目录内，并且必须有 AI 回复中的明确关联。

从 Releases 下载 Mac 应用，放入“应用程序”后打开；自定义配置位置可在应用中选择。源码启动可使用 `deploy/start_mac.py --config <mac-runtime.json>`。`--status` 只读，`--stop` 停止，`--restart` 重启；日志在配置旁的 `logs/`。关闭应用窗口后后台服务继续运行。

Mac 安装包同时支持 Apple Silicon / Intel、macOS 13+，携带连接器代码，升级无需重新下载源码。默认每六小时检查 GitHub 稳定版，也可点“检查更新”；发现新版后点“更新并重启”。更新会保留配置、校验下载和代码签名，并在连接器启动失败时回退。安装包当前使用 ad-hoc 签名，尚未 Apple 公证；后续可在发布工作流中接入自己的 Developer ID。

使用自定义配置目录时，新版本首次打开会让你选择一次原配置文件夹。选择原目录后继续启动；取消可稍后重选，界面不会无限等待目录授权。GitHub 下载遇到临时网络故障会有界限地重试，校验失败始终停止更新。

DeepSeek 使用 `deploy/install_deepseek.py` 注册本机 companion，仅将当前桌面 Host 的正常认证入口写入私有文件，不上传登录 token/cookie。

### Codex 对话目标

打开聊天后，点击顶部的靶心图标。进行中的目标会在消息上方显示一行摘要；详情包含状态、已用 tokens、可选预算和处理时间。暂停会停止目标的自动推进，当前轮次可能继续；关闭会移除目标，保留对话消息。继续或创建活动目标会恢复自动执行。

目标控制使用 Codex 的 `CODEX_CLI_PATH` 启动配置接入原桌面进程的 stdio；不修改 Codex 安装包，不另开 SDK 对话。首次启用需先正常退出 Codex，再点击 Mac AIRemote 的“通过目标通道启动 Codex”。之后在 Codex 打开原有聊天。这个入口不会自动关闭运行中的 Codex 或任务。日常从此按钮启动 Codex；直接打开原 Codex 图标时，目标仍可读取，修改按钮会提示启用通道。

源码方式：`python3 deploy/setup_goal_channel.py --prepare` 只准备私有启动器；`--launch` 在 Codex 未运行时以单次进程环境启动，`--status` 只读检查。配置、随机令牌和本用户 Unix socket 位于 `~/.airemote/goal-channel/`，均限制为本用户访问。目标写入只支持已由该原进程加载的聊天，且仅开放 goal get/set/clear；所有 native 初始化、回复、通知和审批原样转发。通道不会代替用户处理审批。

当前目标通道匹配 Codex `26.1111.21308`，仍属实验适配；首次启用后应在专用测试聊天核对。未验证版本保留正常 Codex 启动，并禁用目标通道。改动前校验目标内容与状态的版本摘要，避免旧页面覆盖；Codex 没有原子条件写入接口，桌面与手机同时操作仍可能竞争。请求重复使用同一幂等键，结果不确定时不盲目重发；页面只有在同一进程回读后才显示已确认。

## Android

下载 Releases 中的 `AIRemote-android.apk`，支持 Android 8+。首次填写自己的 HTTPS 服务器地址，再登录该服务器账号。APK 不内置服务器密码或 agent 凭据。

应用使用同一服务端网页。文件交给系统下载管理器；更新通过 GitHub Releases 检查，并校验 SHA-256、包名、版本和现有安装签名，安装步骤由系统处理。

系统栏与刘海安全区由原生容器处理一次，向 WebView 传递归零后的对应 insets；输入法信息继续正常传递，避免顶部重复留白与键盘关闭后的残留空白。实现依据 [Android WebView window insets](https://developer.android.com/develop/ui/views/layout/webapps/understand-window-insets)。

本地构建需要 JDK 17、Android SDK 35：

```sh
cd android
./gradlew testDebugUnitTest assembleDebug
```

正式包使用长期签名密钥，通过 `AIREMOTE_KEYSTORE`、`AIREMOTE_KEYSTORE_PASSWORD`、`AIREMOTE_KEY_ALIAS`、`AIREMOTE_KEY_PASSWORD` 提供。不要丢失签名密钥，否则原应用无法直接升级。

## 开发与发布

```sh
python3 -m unittest discover -s poc -p 'test_*.py'
python3 -m unittest discover -s relay -p 'test_*.py'
python3 -m unittest discover -s deploy -p 'test_*.py'
npm --prefix web ci
npm --prefix web test
npm --prefix web run build
```

真实写入验证脚本必须显式提供授权聊天 ID；单元测试使用隔离合成数据。不要将聊天数据库、截图、日志或测试产物加入 Git。

版本号在 `VERSION`。GitHub Actions 执行回归与构建。签名私钥仅放在维护者私有目录和 Actions Secrets。

推送 `v<VERSION>` 标签会执行正式发布工作流：测试通过后构建签名 APK、通用 Mac 应用和包含网页的中继包，统一生成 `SHA256SUMS`，最后发布完整的 GitHub Release。Android 签名通过仓库 Secrets `ANDROID_KEYSTORE_BASE64`、`ANDROID_KEYSTORE_PASSWORD`、`ANDROID_KEY_ALIAS`、`ANDROID_KEY_PASSWORD` 配置；前三者中的 alias 应与 keystore 一致。只在受信任的发布标签中使用这些 Secrets，fork 需配置自己的签名。

```sh
python3 macos/build_app.py --zip dist/AIRemote-macos.zip
python3 tools/package_server.py
git tag "v$(cat VERSION)"
git push origin "v$(cat VERSION)"
```

下载文件和 `SHA256SUMS` 后，Linux 可用 `sha256sum -c SHA256SUMS`，Mac 可用 `shasum -a 256 -c SHA256SUMS`。Mac / 服务端的信任基础是维护者的 GitHub Releases 和 HTTPS；Android 还会要求更新包与原安装包签名一致。自动检查默认开启，Android 安装仍需用户在系统界面确认。

## 范围与限制

已验证 Codex 桌面 `26.1103.60933`、`26.1106.51428`、`26.1111.21308`，DeepSeek Harness `0.2.2-rc.1`。内部 IPC/Host API 不是稳定公开协议；未验证版本可能只读，写入会停止，而不是启动另一运行实例。

消息按需轮询，不承诺零延迟逐字流式。Mac 休眠或断网会显示离线，不会误判为任务完成。中继不是端到端加密，请保护自己的服务器和配置备份。

## 许可与参考

原创代码采用 [MIT](LICENSE)。依赖与品牌标识见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

- [GitHub Releases API](https://docs.github.com/en/rest/releases/releases)
- [Android PackageManager](https://developer.android.com/reference/android/content/pm/PackageManager)
- [Android FileProvider](https://developer.android.com/reference/androidx/core/content/FileProvider)

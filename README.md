# AIRemote

从手机继续 Mac 上的 AI 对话。支持网页、Android APK 和 Mac 连接器应用，可部署到自己的服务器。

[下载最新版本](https://github.com/Winlifes/AIRemote/releases/latest) · [MIT License](LICENSE) · [安全说明](SECURITY.md)

## 功能

- 读取 Mac Codex 原有对话、真实运行状态、回复和关联文件；向同一个桌面对话发送指令。
- 原生项目层级，展开项目才读取列表；打开聊天才读取最新消息，上翻才读取更早历史。
- 原生队列添加、编辑、删除、排序、清空和立即执行，以及新建真实对话。
- DeepSeek Harness 现有 Host 的项目、聊天、续聊、文件、新建和队列；其当前版本没有排序接口。
- Claude Code 本地项目、已有会话、运行状态、历史分页及关联文件。配置 CLI 的 API 凭据后，可继续原会话或新建。
- 手机抽屉侧栏、胶囊输入框、Markdown、用户消息右对齐和真实处理时间。
- 侧栏与全屏遮罩同时过渡；当前 Codex 模型、思考强度、工作/计划模式及权限从原桌面 owner 实时读取。
- Mac 应用启动/停止连接器；Android 支持系统文件下载和 GitHub Releases 更新。
- Codex 对话目标：查看状态、目标消耗与预算，创建/修改、暂停/继续及关闭；在原桌面 app-server 上写入并回读确认。

CodeBuddy、多台 Mac、图片和音频指令、手机处理 AI 审批仍待扩展。

## 架构

```text
Android / 手机浏览器 --HTTPS--> 自托管 relay <--HTTPS 主动轮询-- Mac agent
                                                                  |
                                                          loopback connector
                                                                  |
                                     Codex owner / DeepSeek Host / Claude Code CLI
```

Mac 主动连接中继。Codex 和 DeepSeek 由原桌面进程执行。Claude Code 使用原会话 ID 从 CLI 续聊。

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

Mac 安装包同时支持 Apple Silicon / Intel、macOS 13+，携带连接器代码，升级无需重新下载源码。默认每六小时检查 GitHub 稳定版，也可点“检查更新”；发现新版后点“更新并重启”。检查、下载、校验、解压和安装分别显示阶段，下载时展示百分比、已下载字节和速率。更新会保留配置、校验下载和代码签名，并在连接器启动失败时回退。安装包当前使用 ad-hoc 签名，尚未 Apple 公证；后续可在发布工作流中接入自己的 Developer ID。

Mac 应用打开时自动使用上次保存的配置目录。首次使用时从 `~/Library/Application Support/AIRemote` 读取默认配置。配置缺失或权限受限时，应用会显示尝试读取的位置，点击“配置文件夹…”可以打开保存的位置或更换目录。GitHub 下载遇到临时网络故障会有界限地重试，校验失败始终停止更新。

DeepSeek 使用 `deploy/install_deepseek.py` 注册本机 companion，仅将当前桌面 Host 的正常认证入口写入私有文件，不上传登录 token/cookie。

### Claude Code

在网页或 Flutter 应用的侧栏点击 Claude 图标。可读取授权目录下的本地 Claude Code 会话。开启 `--discover-desktop-projects` 后，也会显示 Claude Desktop 的 Code 页中的本地会话。会话历史按需翻页，运行状态来自 `claude agents --json`。文件下载限于会话回复明确链接的授权文件。

续聊和新建需要 Mac 上的官方 Claude Code CLI，可使用独立安装或 Claude Desktop 自带的版本。给 CLI 配置自己的 API 凭据，例如从已设置 `ANTHROPIC_API_KEY` 的本机终端启动连接器。桌面仍打开同一会话时可查看消息和状态；关闭会话后，可从手机在已授权的工作目录中按原会话 ID 继续。新建会话需填写第一条指令。回执确认真实写入的消息和回复后才显示完成。

本版覆盖 Claude Code 本地会话。Claude Chat、Cowork、云端会话和原生队列待适配。

- [Claude Code Desktop](https://code.claude.com/docs/en/desktop)
- [Claude Code CLI 认证说明](https://code.claude.com/docs/en/legal-and-compliance#authentication-and-credential-use)

### Codex 对话目标

打开聊天后，点击顶部的靶心图标。目标状态条直接附在输入框上方，突出目标内容，状态和处理时间合并在第二行；可直接暂停/继续，通过“更多”查看详情、修改或关闭目标。详情包含已用 tokens 和可选预算。暂停会停止目标的自动推进，当前轮次可能继续；关闭会移除目标，保留对话消息。继续或创建活动目标会恢复自动执行。

目标控制使用 Codex 的 `CODEX_CLI_PATH` 启动配置接入原桌面进程的 stdio；不修改 Codex 安装包，不另开 SDK 对话。首次启用需先正常退出 Codex，再点击 Mac AIRemote 的“通过目标通道启动 Codex”。之后在 Codex 打开原有聊天。这个入口不会自动关闭运行中的 Codex 或任务。日常从此按钮启动 Codex；直接打开原 Codex 图标时，目标仍可读取，修改按钮会提示启用通道。

源码方式：`python3 deploy/setup_goal_channel.py --prepare` 只准备私有启动器；`--launch` 在 Codex 未运行时以单次进程环境启动，`--status` 只读检查。配置、随机令牌和本用户 Unix socket 位于 `~/.airemote/goal-channel/`，均限制为本用户访问。目标写入只支持已由该原进程加载的聊天，且仅开放 goal get/set/clear；所有 native 初始化、回复、通知和审批原样转发。通道不会代替用户处理审批。

当前目标通道匹配 Codex `26.1111.21308`，仍属实验适配；首次启用后应在专用测试聊天核对。未验证版本保留正常 Codex 启动，并禁用目标通道。改动前校验目标内容与状态的版本摘要，避免旧页面覆盖；Codex 没有原子条件写入接口，桌面与手机同时操作仍可能竞争。请求重复使用同一幂等键，结果不确定时不盲目重发；页面只有在同一进程回读后才显示已确认。

0.7.1 修正桌面握手兼容：观察并转发原生 `initialize` 成功响应后启用通道，不等待 SDK 才会发送的 `initialized` 通知。0.7.0 已启动的透明代理仍在运行旧代码，更新后需要通过 Mac AIRemote 再次正常打开 Codex；单独重启 AIRemote 服务不能替换 Codex 持有的 stdio 代理。

## Android / iOS（Flutter）

Android 0.8.1 起使用 Flutter 组件渲染全部界面：原生路由、侧栏、聊天列表、输入框、队列和目标表单。客户端直接通过 HTTPS API 接入自己的中继，不加载网页 UI；React 网页仍可单独访问。

下载 Releases 中的 `AIRemote-android.apk`，支持 Android 8+。包名和长期签名保留，原版本可覆盖升级。首次填写自己的 HTTPS 服务器地址，再登录该服务器账号；旧版本的地址和登录会话会迁移到加密存储。APK 不内置服务器密码或 agent 凭据。

侧栏和遮罩覆盖整屏，内容控件避开系统栏和刘海；不在原生容器外层加空白安全区。键盘通过 Flutter 布局调整。输入框聚焦时从窄胶囊平滑展开；点击空白处或收起键盘后收回，保留草稿和光标。动画期间使用同一个输入控件，支持中文输入状态；普通输入框不显示工作提示文字。项目展开后才拉取对话，选中聊天后才轮询最新消息、状态和队列，向上翻页才读取历史；前台恢复时重新同步，后台停止轮询。

文件交给 Android 系统下载管理器；iOS 使用系统分享面板保存文件。更新来自 GitHub Releases，持续显示百分比、已下载大小、总大小和速率；源站没有提供大小时显示不定进度。下载与 SHA-256 校验、安装包签名验证分别显示状态。关闭设置面板不会取消下载，再次打开可查看进度。系统安装需用户确认，并验证包名、递增版本和原安装签名。

iOS 工程与 Android 共用 `lib/` 的界面、同步、队列、目标和下载代码，CI 验证模拟器构建。真机安装、TestFlight / App Store 发布仍需维护者配置 Apple 开发者签名；iOS 更新走苹果分发渠道。

本地构建：Flutter **3.50.0**（框架提交 `ab2912035e160d9cad6306df307576da6128ebdb`）、JDK 21、Android SDK 36 / NDK 28.2.13676358；iOS 需 macOS 和 Xcode。

```sh
flutter pub get --enforce-lockfile
flutter analyze
flutter test
flutter build apk --debug
flutter build ios --simulator --no-codesign
```

正式 APK 使用长期签名密钥，通过 `AIREMOTE_KEYSTORE`、`AIREMOTE_KEYSTORE_PASSWORD`、`AIREMOTE_KEY_ALIAS`、`AIREMOTE_KEY_PASSWORD` 提供，再运行 `flutter build apk --release`。不要丢失签名密钥，否则原应用无法直接升级。

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

版本号在 `VERSION`，同步 `pubspec.yaml` 与网页 package 文件；Android versionCode 使用 `major * 1000000 + minor * 1000 + patch`。GitHub Actions 执行回归与构建。签名私钥仅放在维护者私有目录和 Actions Secrets。

常规 CI 和签名发布使用固定的更新响应测试版本解析、下载和 SHA-256 校验。需要核实 GitHub 上已发布的更新时，在 **Build and test** 工作流手动启用 **Check live update feed**，或在本机执行 `flutter test test_live/github_update_test.dart`。

推送 `v<VERSION>` 标签会执行正式发布工作流：测试通过后构建签名 APK、通用 Mac 应用和包含网页的中继包，同时验证 iOS 模拟器构建，统一生成 `SHA256SUMS`，最后发布完整的 GitHub Release。Android 签名通过仓库 Secrets `ANDROID_KEYSTORE_BASE64`、`ANDROID_KEYSTORE_PASSWORD`、`ANDROID_KEY_ALIAS`、`ANDROID_KEY_PASSWORD` 配置；前三者中的 alias 应与 keystore 一致。只在受信任的发布标签中使用这些 Secrets，fork 需配置自己的签名。

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

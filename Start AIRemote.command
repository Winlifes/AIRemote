#!/bin/zsh
set -u
TASK_PROJECT_DIR="${0:A:h}"
TASK_START_STATUS=0
printf '\n正在检查并启动 AIRemote…\n\n'
if /usr/bin/python3 -u "$TASK_PROJECT_DIR/deploy/start_mac.py" --config "$TASK_PROJECT_DIR/.local/mac-runtime.json"; then
  printf '\n启动检查通过。请打开配置中的手机网页地址。\n'
  printf '连接器在后台运行，可以关闭这个窗口。\n'
else
  TASK_START_STATUS=$?
  printf '\n启动未完成，请保留上面的报错。\n'
  printf '日志目录：%s/.local/logs\n' "$TASK_PROJECT_DIR"
  printf '如果报 Operation not permitted，请在系统设置 → 隐私与安全性 → 文件与文件夹中，允许终端访问“文稿”文件夹，再重试。\n'
fi
if [[ -t 0 && "${AIREMOTE_NONINTERACTIVE:-0}" != "1" ]]; then
  printf '\n按回车键关闭此窗口…'
  IFS= read -r TASK_DISMISS || true
fi
exit "$TASK_START_STATUS"

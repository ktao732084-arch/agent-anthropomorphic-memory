#!/usr/bin/env bash
# 下载长期记忆评测用到的公开数据集（只取需要的文件），用法：
#   bash eval/longterm/fetch_datasets.sh DATA_DIR
# 数据不随本仓库分发，使用前请阅读各数据集的许可证。
set -euo pipefail

DATA_DIR="${1:?用法: fetch_datasets.sh DATA_DIR}"
mkdir -p "$DATA_DIR"
cd "$DATA_DIR"

fetch() {  # fetch <github repo> <dir> <sparse path>...
  local repo="$1" dir="$2"
  shift 2
  if [ ! -d "$dir/.git" ]; then
    git clone -q --depth 1 --filter=blob:none --no-checkout "https://github.com/$repo" "$dir"
  fi
  git -C "$dir" sparse-checkout set --no-cone "$@"
  git -C "$dir" checkout -q
}

fetch zhongwanjun/MemoryBank-SiliconFriend MemoryBank-SiliconFriend '/eval_data/cn/'
fetch Elvin-Yiming-Du/PerLTQA PerLTQA '/Dataset/zh/'
fetch snap-research/locomo locomo '/data/locomo10.json'
echo "已下载到 $DATA_DIR"

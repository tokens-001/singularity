# SD 卡备份规则

- 每周日执行
- 先把Singularity-current 复制为Singularity-YYYY-MM-DD 快照，再 rsync 最新版本到Singularity-current
- 命令：`rsync -av --delete ~/Singularity/ /Volumes/Singularity备份/Singularity-current/`
- 沙箱无法读写 SD 卡时提醒用户手动执行

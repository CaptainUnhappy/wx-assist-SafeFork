# wx-assist-SafeFork

这是 [`MaleleStudySpace/wx-assist`](https://github.com/MaleleStudySpace/wx-assist) 的 SafeFork 镜像。

## 分支用途

- `master`、`test`：与上游同名分支保持一致。
- `sync-control`：默认分支，仅保存自动同步工作流，不属于上游源码。

## 同步规则

- 每小时检查一次，也可在 Actions 中手动执行。
- 只创建缺失分支、快进已有分支、复制缺失 Tag。
- 遇到分叉历史或同名异 SHA 的 Tag 会停止，不会强制覆盖。
- 不删除 Fork 独有分支或 Tag，不创建自动备份分支。

查看源码时请切换到 [`master`](../../tree/master)。

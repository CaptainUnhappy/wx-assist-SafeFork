# wx-assist-SafeFork

这是 [`MaleleStudySpace/wx-assist`](https://github.com/MaleleStudySpace/wx-assist) 的 SafeFork。

## 分支用途

- `master`、`test`：快进同步上游同名分支，保留已有提交历史。
- `sync-control`：默认分支，仅保存自动同步工作流，不属于上游源码。

## 同步规则

- 计划每小时检查一次，也可在 Actions 中手动执行；GitHub 可能延迟或跳过定时运行。
- 只创建缺失分支、快进已有分支、复制缺失 Tag。
- 遇到分叉历史或未经审核的同名异 SHA 标签时停止，不会强制覆盖。
- 不删除 Fork 独有分支或 Tag，不创建自动备份分支。
- 经核对的标签差异可锁定两端 SHA，保留原标签并继续其他安全更新；任一 SHA 改变时重新停止。

## 保留的标签

`v1.7.0` 保留原始附注标签对象 `23ddaa7846ede6d97b852601bdeb356e334d40e3`，指向提交 `02eb97addc98ee79e74869cc1605f6f028d1c974`。上游已将同名标签改为指向 `49ec2fe5ff56ad490cbdb548322aa496a6be30fa` 的轻量标签。

这组差异记录于仓库变量 `SAFEFORK_PRESERVED_TAGS`。同步会持续报告差异，不移动原标签。完整规则见 [SafeFork 规范](https://github.com/CaptainUnhappy/SafeFork/blob/main/skills/safefork/references/spec.md)。

查看源码时请切换到 [`master`](../../tree/master)。

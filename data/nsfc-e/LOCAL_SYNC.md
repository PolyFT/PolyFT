# 本地同步

完整 CSV、SQLite、原始 JSONL 和规则输出保存在 GitHub Release：
`nsfc-e-official-completed-2026-08-26`。

在仓库根目录执行：

```bash
python tools/sync_nsfc_e_local.py --dest data-local/nsfc-e
```

脚本会下载发布包、核验 SHA-256，并原子更新 `data-local/nsfc-e/current`。
仓库中的 `data/nsfc-e/` 只保存便于 Git 同步和审查的规则、统计、覆盖度及报告。
不接入百度网盘或其他网盘。

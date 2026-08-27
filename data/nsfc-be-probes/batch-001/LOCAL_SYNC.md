# 本地同步

完整原始API响应和逐论文证据保存在Release：
`nsfc-be-probe-batch-001-2026-08-27`。

在仓库根目录执行：

```bash
python tools/sync_nsfc_be_probe_local.py \
  --dest data-local/nsfc-be-probes/batch-001
```

脚本会核验SHA-256并原子更新本地目录。

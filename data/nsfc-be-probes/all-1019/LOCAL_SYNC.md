# 本地同步

完整1,019个候选的原始API响应、逐论文证据和累计状态保存在Release：
`nsfc-be-probe-all-1019-2026-08-27`。

在仓库根目录执行：

```bash
python tools/sync_nsfc_be_probe_all_local.py \
  --dest data-local/nsfc-be-probes/all-1019
```

脚本会核验SHA-256并原子更新本地目录。
